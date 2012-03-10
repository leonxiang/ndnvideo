import pygst
pygst.require("0.10")
import gst
import gobject

import math, Queue, threading, struct

import pyccn
import utils

__all__ = ["CCNPacketizer", "CCNDepacketizer"]

# left, offset, element count
packet_hdr = "!BHB"
packet_hdr_len = struct.calcsize(packet_hdr)

# size, timestamp, duration
segment_hdr = "!IQQ"
segment_hdr_len = struct.calcsize(segment_hdr)

CMD_SEEK = 1

def debug(cls, text):
	print "%s: %s" % (cls.__class__.__name__, text)

class DataSegmenter(object):
	def __init__(self, callback, max_size = None):
		global packet_hdr_len

		self._callback = callback
		self._max_size = None if max_size is None else max_size - packet_hdr_len

		self._packet_content = bytearray()
		self._packet_elements = 0
		self._packet_element_off = 0
		self._packet_lost = False

	@staticmethod
	def buffer2segment(buffer):
		global segment_hdr, segment_hdr_len

		return struct.pack(segment_hdr, buffer.size, buffer.timestamp, \
				buffer.duration) + buffer.data

	@staticmethod
	def segment2buffer(segment, offset):
		global segment_hdr, segment_hdr_len

		if len(segment) - offset < segment_hdr_len:
			return None, offset

		header = bytes(segment[offset:offset + segment_hdr_len])
		size, timestamp, duration = struct.unpack(segment_hdr, header)
		start = offset + segment_hdr_len
		end = offset + segment_hdr_len + size

		if end > len(segment):
			return None, offset

		buf = gst.Buffer(bytes(segment[start:end]))
		buf.timestamp, buf.duration = timestamp, duration

		return buf, end

	def process_buffer(self, buffer, start_fresh = False, flush = False):
		assert self._max_size, "You can't use process_buffer without defining max_size"

		if start_fresh and len(self._packet_content) > 0:
			self.perform_send_callback(0)

		segment = self.buffer2segment(buffer)
		self._packet_content.extend(segment)
		self._packet_elements += 1

		nochunks = int(math.ceil(len(self._packet_content) \
				/ float(self._max_size)))

		while nochunks >= 2:
			#assert(nochunks > 0)
			packet_size = min(self._max_size, len(self._packet_content))
			nochunks -= 1
			self.perform_send_callback(nochunks, packet_size)
		assert(nochunks == 1)

		if len(self._packet_content) == self._max_size or flush:
			self.perform_send_callback(0)

	def process_buffer_split(self, buffer):
		global packet_hdr

		segment = self.buffer2segment(buffer)
		segment_size = len(segment)

		nochunks = int(math.ceil(segment_size / float(self._max_size)))

		data_off = 0
		while data_off < segment_size:
			assert(nochunks > 0)

			data_size = min(self._max_size, segment_size - data_off)
			chunk = segment[data_off:data_off + data_size]
			data_off += data_size

			nochunks -= 1
			header = struct.pack(packet_hdr, nochunks, 0, 0)
			self._callback(header + chunk)
		assert(nochunks == 0)

	def packet_lost(self):
		self._packet_lost = True
		self._packet_content = bytearray()
		self._packet_elements = 0

	def process_packet(self, packet):
		global packet_hdr, packet_hdr_len

		header = packet[:packet_hdr_len]
		left, offset, count = struct.unpack(packet_hdr, header)

		if not self._packet_lost or len(self._packet_content) > 0:
			offset = 0

		offset += packet_hdr_len
		self._packet_content.extend(packet[offset:])
		self._packet_elements += count

		off = 0
		while self._packet_elements > 0:
			buf, off = self.segment2buffer(self._packet_content, off)

			if buf is None:
				break

			if self._packet_lost:
				buf.flag_set(gst.BUFFER_FLAG_DISCONT)
				self._packet_lost = False

			self._callback(buf)
			self._packet_elements -= 1
		assert (left > 0 and self._packet_elements == 1) or self._packet_elements == 0, "left = %d, packet_elements = %d" % (left, self._packet_elements)
		#assert self._packet_elements <= 1, "packet_elements %d" % self._packet_elements

		self._packet_content = self._packet_content[off:]

	def perform_send_callback(self, left, size = None):
		if size is None:
			size = len(self._packet_content)

		header = struct.pack(packet_hdr, left, self._packet_element_off, \
				self._packet_elements)

		self._callback(header + bytes(self._packet_content[:size]))
		self._packet_content = self._packet_content[size:]
		self._packet_element_off = len(self._packet_content)
		self._packet_elements = 0

class CCNPacketizer(object):
	def __init__(self, publisher, uri):
		self._chunk_size = 4096
		self._segment = 0
		self._caps = None

		self.publisher = publisher

		self._basename = pyccn.Name(uri)
		self._name_segments = self._basename.append("segments")
		self._name_frames = self._basename.append("index")
		self._name_key = self._basename.append("key")

		self._key = pyccn.CCN.getDefaultKey()
		self._signed_info = pyccn.SignedInfo(self._key.publicKeyID, pyccn.KeyLocator(self._name_key))
		self._signed_info_frames = pyccn.SignedInfo(self._key.publicKeyID, pyccn.KeyLocator(self._name_key))

		self._segmenter = DataSegmenter(self.send_data, self._chunk_size)

		signed_info = pyccn.SignedInfo(self._key.publicKeyID, pyccn.KeyLocator(self._key))
		co = pyccn.ContentObject(self._name_key, self._key.publicToDER(), signed_info)
		co.sign(self._key)
		self.publisher.put(co)

	def set_caps(self, caps):
		if not self._caps:
			self._caps = caps

			packet = self.prepare_stream_info_packet(caps)
			self.publisher.put(packet)

			self.post_set_caps(caps)

	def post_set_caps(self, caps):
		pass

	def prepare_stream_info_packet(self, caps):
		name = self._basename.append("stream_info")

		co = pyccn.ContentObject(name, self._caps, self._signed_info)
		co.sign(self._key)

		return co

	def prepare_frame_packet(self, frame, segment):
		name = self._name_frames.append(frame)

		co = pyccn.ContentObject(name, segment, self._signed_info_frames)
		co.sign(self._key)

		return co

	def send_data(self, packet):
		name = self._name_segments.appendSegment(self._segment)
		self._segment += 1

		co = pyccn.ContentObject(name, packet, self._signed_info)
		co.sign(self._key)
		self.publisher.put(co)

	def pre_process_buffer(self, buffer):
		return True, True

	def process_buffer(self, buffer):
		result = self.pre_process_buffer(buffer)
		self._segmenter.process_buffer(buffer, start_fresh = result[0],
				flush = result[1])

class CCNDepacketizer(pyccn.Closure):
	def __init__(self, uri, window = None, timeout = 1.0, retries = 1):
		window = window if window is not None else 1
		self.interest_lifetime = timeout if timeout is not None else 1.0
		self.interest_retries = retries

		self.queue = Queue.Queue(window * 2)
		self.duration_ns = None

		self._running = False
		self._caps = None
		self._seek_segment = None
		self._duration_last = None
		self._cmd_q = Queue.Queue(2)

		self._handle = pyccn.CCN()
		self._get_handle = pyccn.CCN()

		self._uri = pyccn.Name(uri)
		self._name_segments = self._uri + 'segments'
		self._name_frames = self._uri + 'index'

		self._pipeline = utils.PipelineFetch(window, self.issue_interest,
				self.process_response)
		self._segmenter = DataSegmenter(self.push_data)

		self._stats_retries = 0
		self._stats_drops = 0

		self._tmp_retry_requests = {}

	def set_window(self, window):
		self._pipeline.window = window

	def fetch_stream_info(self):
		name = self._uri.append('stream_info')
		debug(self, "Fetching stream_info from %s ..." % name)

		co = self._get_handle.get(name)
		if not co:
			debug(self, "Unable to fetch %s" % name)
			exit(10)

		self._caps = gst.caps_from_string(co.content)
		debug(self, "Stream caps: %s" % self._caps)

		self.post_fetch_stream_info(self._caps)

	def post_fetch_stream_info(self, caps):
		pass

	def get_caps(self):
		if not self._caps:
			self.fetch_stream_info()

		return self._caps

	def start(self):
		self._receiver_thread = threading.Thread(target = self.run)
		self._running = True
		self._receiver_thread.start()

	def stop(self):
		self._running = False
		self.finish_ccn_loop()
		debug(self, "Waiting for ccn to shutdown")
		self._receiver_thread.join()
		debug(self, "Shot down")

	def finish_ccn_loop(self):
		self._handle.setRunTimeout(0)

	def seek(self, ns):
		self._cmd_q.put([CMD_SEEK, ns])
		self.finish_ccn_loop()

#
# Bellow methods are called by thread
#

	def run(self):
		debug(self, "Running ccn loop")
		self.check_duration()

		iter = 0
		while self._running:
			if iter > 5:
				iter = 0
				self.check_duration()

			self._handle.run(2000)
			self.process_commands()
			iter += 1

		debug(self, "Finished running ccn loop")

	def process_commands(self):
		try:
			if self._cmd_q.empty():
				return
			cmd = self._cmd_q.get_nowait()
		except Queue.Empty:
			return

		if cmd[0] == CMD_SEEK:
			tc, segment = self.fetch_seek_query(cmd[1])
			debug(self, "Seeking to segment %d [%s]" % (segment, tc))
			self._seek_segment = True
			self._segmenter.packet_lost()
			self._pipeline.reset(segment)
			self._cmd_q.task_done()
		else:
			raise Exception, "Unknown command: %d" % cmd

	def ts2index(self, ts):
		return pyccn.Name.num2seg(ts)

	def ts2index_add_1(self, ts):
		return self.ts2index(ts + 1)

	def index2ts(self, index):
		return pyccn.Name.seg2num(index)

	def fetch_seek_query(self, ns):
		index = self.ts2index_add_1(ns)

		#debug(self, "Fetching segment number before %s" % index)

		interest = pyccn.Interest(childSelector = 1,
			answerOriginKind = pyccn.AOK_NONE)
		interest.exclude = pyccn.ExclusionFilter()
		interest.exclude.add_name(pyccn.Name([index]))
		interest.exclude.add_any()

		#debug(self, "Sending interest to %s" % self._name_frames)
		#debug(self, "Exclusion list %s" % interest.exclude)
		while True:
			co = self._get_handle.get(self._name_frames, interest)
			if co:
				break
			debug(self, "Timeout while seeking %d, retrying ..." % (ns))
		debug(self, "Got segment: %s" % co.content)

		index = co.name[-1]
		segment = int(co.content)

		return (self.index2ts(index), segment)

	def check_duration(self):
		interest = pyccn.Interest(childSelector = 1,
			answerOriginKind = pyccn.AOK_NONE)

		if self._duration_last:
			interest.exclude = pyccn.ExclusionFilter()
			interest.exclude.add_any()
			interest.exclude.add_name(pyccn.Name([self._duration_last]))

		co = self._get_handle.get(self._name_frames, interest, 100)
		if co:
			self._duration_last = co.name[-1]
			#debug(self, ">%r< (%f)" % (self._duration_last, self.index2ts(self._duration_last) / 1000000000.))
		else:
			debug(self, "No response received for duration request")

		if self._duration_last:
			self.duration_ns = self.index2ts(self._duration_last)
		else:
			self.duration_ns = 0

	def issue_interest(self, segment):
		name = self._name_segments.appendSegment(segment)

		#debug(self, "Issuing an interest for: %s" % name)
		self._tmp_retry_requests[str(name[-1])] = self.interest_retries

		interest = pyccn.Interest(interestLifetime = self.interest_lifetime)
		self._handle.expressInterest(name, self, interest)

		return True

	def process_response(self, co):
		if not co:
			self._segmenter.packet_lost()
			return

		self._segmenter.process_packet(co.content)

	def push_data(self, buf):
		status = 0

		# Marking jump due to seeking
		if self._seek_segment == True:
			debug(self, "Marking as discontinued")
			status = CMD_SEEK
			self._seek_segment = None

		while True:
			try:
				self.queue.put((status, buf), True, 1)
				break
			except Queue.Full:
				if not self._running:
					break

	def upcall(self, kind, info):
		if not self._running:
			return pyccn.RESULT_OK

		elif kind == pyccn.UPCALL_FINAL:
			return pyccn.RESULT_OK

		elif kind == pyccn.UPCALL_CONTENT:
			self._pipeline.put(pyccn.Name.seg2num(info.ContentObject.name[-1]),
							info.ContentObject)
			return pyccn.RESULT_OK

		elif kind == pyccn.UPCALL_INTEREST_TIMED_OUT:
			name = str(info.Interest.name[-1])

			if self._tmp_retry_requests[name]:
				#debug(self, "timeout for %s - re-expressing" % info.Interest.name)
				self._stats_retries += 1
				self._tmp_retry_requests[name] -= 1
				return pyccn.RESULT_REEXPRESS

			#debug(self, "timeout for %r - skipping" % name)
			self._stats_drops += 1
			del self._tmp_retry_requests[name]
			self._pipeline.timeout(pyccn.Name.seg2num(info.Interest.name[-1]))
			return pyccn.RESULT_OK

		elif kind == pyccn.UPCALL_CONTENT_UNVERIFIED:
			debug(self, "%s arrived unverified, fetching the key" % info.ContentObject.name)
			return pyccn.RESULT_VERIFY

		debug(self, "Got unknown kind: %d" % kind)

		return pyccn.RESULT_ERR

	def get_status(self):
		return "Pipeline size: %d/%d Position: %d Retries: %d Drops: %d Duration: %ds" \
			% (self._pipeline.get_pipeline_size(), self._pipeline.window,
			self._pipeline.get_position(), self._stats_retries, self._stats_drops,
			self.duration_ns / gst.SECOND if self.duration_ns else 1.0)
