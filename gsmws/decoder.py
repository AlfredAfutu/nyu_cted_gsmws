"""
This file is part of GSMWS.
"""

import gsm
import collections
import threading
import logging
import time
import datetime
import Queue
import sqlite3
import zmq
from sets import Set

class MeasurementReportList(object):
    def __init__(self, maxlen=10000):
        self.lock = threading.Lock()
        self.maxlen = maxlen
        self.reports = collections.deque(maxlen=maxlen)

    def put(self, report):
        with self.lock:
            self.reports.append(report)

    def get(self):
        with self.lock:
            self.reports.popleft()

    def getall(self):
        with self.lock:
            reports, self.reports = self.reports, collections.deque(maxlen=self.maxlen)
        return list(reports)

class EventDecoder(threading.Thread):
    """
    The EventDecoder listens for PhysicalStatus API events from OpenBTS and
    stores them in an in-memory MeasurementReportList. Unlike GSMDecoder, the
    EventDecoder does no further processing on them -- they are passed along
    as-is for interpretation later. We don't even decode the JSON, as these are
    intended to be pulled via an API from a BTS, so why bother?
    """
    def __init__(self, host="tcp://localhost:45160", maxlen=1000, loglvl=logging.INFO):
        threading.Thread.__init__(self)
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s',
                            filename='/var/log/gsmws.log',level=loglvl)

        # Connect to OpenBTS event stream
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.connect(host)
        self.socket.setsockopt(zmq.SUBSCRIBE, "")

        self.reports = MeasurementReportList(maxlen)

    def run(self):
        """
        Main processing loop. Run forever!
        """
        while True:
            msg = self.socket.recv()
            self.reports.put(msg)


class GSMDecoder(threading.Thread):
    """
    DEPRECATED

    This is responsible for managing the packet stream from tshark, processing
    reports, and storing the data.
    """


    def __init__(self, stream, db_lock, gsmwsdb_location, nct, maxlen=100, loglvl=logging.INFO, decoder_id=0):
        threading.Thread.__init__(self)
        self.stream = stream
        self.current_message = ""
        self.current_arfcn = None
        self.num_of_cells = None
        self.last_arfcns = []
        self.ncc_permitted = None
        self.ignore_reports = False # ignore measurement reports
        self.msgs_seen = 0

        self.runtime = {}
        self.runtime["initial_time"] = None
        self.runtime["arfcns"] = []
        self.runtime["rssis"] = []
        self.runtime["timestamp"] = []
        self.runtime["arfcn_tracking"] = [False, False, False, False, False]
        self.NEIGHBOR_CYCLE_TIME = nct
        self.gsmwsdb_lock = db_lock
        self.gsmwsdb_location = gsmwsdb_location
        self.gsmwsdb = None # this gets created in run()

        self.decoder_id = decoder_id

        self.rssi_queue = Queue.Queue()

        self.reports = MeasurementReportList()

        self.strengths_maxlen = maxlen
        self.max_strengths = {} # max strength ever seen for a given arfcn
        self.recent_strengths = {} # last 100 measurement reports for each arfcn
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)
        logging.warn("GSMDecoder is deprecated! Use at your own risk.")


    def _populate_strengths(self):
        """
        Rather than storing our history, we can just store the current mean for
        each ARFCN, plus the number of recent readings we have. On start, we
        just add N instances of each ARFCN's mean to the list. This has the
        downside of being not general (only works with means) and losing
        history potentially (i.e., we die twice in a row: we'll repopulate with
        just the mean value from before).
        """
        # populate the above from stable
        with self.gsmwsdb_lock:
            max_strengths = self.gsmwsdb.execute("SELECT ARFCN, RSSI FROM MAX_STRENGTHS").fetchall()
            for item in max_strengths:
                self.max_strengths[item[0]] = item[1]

            recent = self.gsmwsdb.execute("SELECT ARFCN, RSSI, COUNT FROM AVG_STRENGTHS").fetchall()
            for item in recent:
                self.recent_strengths[item[0]] = collections.deque([item[1] for _ in range(0,item[2])],maxlen=self.strengths_maxlen)

    def __write_rssi(self):
        if not self.rssi_queue.empty():
            with self.gsmwsdb_lock:
                while not self.rssi_queue.empty():
                    try:
                        query = self.rssi_queue.get()
                        self.gsmwsdb.execute(query[0], query[1])
                    except Queue.Empty:
                        break
                self.gsmwsdb.commit()


    def rssi(self):
        # returns a dict with a weighted average of each arfcn
        # we base this only on last known data for an ARFCN -- lack of report
        # doesn't mean anything, but if an arfcn is in the neighbor list and we
        # don't get a report for it, we count that as -1.

        res = {}
        now = datetime.datetime.now()

        for arfcn in self.max_strengths:
            tot = self.max_strengths[arfcn] + sum(self.recent_strengths[arfcn])
            res[arfcn] = float(tot) / (1 + len(self.recent_strengths[arfcn]))

            # now, update the db
            recent_avg = sum(self.recent_strengths[arfcn]) / float(len(self.recent_strengths[arfcn]))
            self.rssi_queue.put(("DELETE FROM AVG_STRENGTHS WHERE ARFCN=?", (arfcn,)))
            self.rssi_queue.put(("INSERT INTO AVG_STRENGTHS VALUES (?, ?, ?, ?)", (now, arfcn, recent_avg, len(self.recent_strengths[arfcn]))))

        return res


    def run(self):
        logging.info("In Decoder run")
        self.gsmwsdb = sqlite3.connect(self.gsmwsdb_location)
        self._populate_strengths()

        last_rssi_update = datetime.datetime.now()

        # Main processing loop. We read output from tshark line by line
        # breaking every time we find a line that is unindented. Unindented
        # line = new message. The message is then handed off to process(),
        # which extracts relevant information from it.
        for line in self.stream:
            self.__write_rssi()
            if line.startswith("    "):
                #print "appending"
                self.current_message += "%s" % line
            else:
                self.process(self.current_message)
                self.current_message = line

    def update_strength(self, strengths):
        self.update_max_strength(strengths)
        self.update_recent_strengths(strengths)

    def update_max_strength(self, strengths):
        with self.gsmwsdb_lock:
            for arfcn in strengths:
                value = strengths[arfcn]
                now = datetime.datetime.now()

                # FIXME potential leak here: we could record max values twice if we're
                # not in sync w/ db, but that should only happen rarely
                if arfcn not in self.max_strengths:
                    self.max_strengths[arfcn] = value
                    self.gsmwsdb.execute("INSERT INTO MAX_STRENGTHS VALUES(?,?,?)", (now, arfcn, value))
                elif value > self.max_strengths[arfcn]:
                    self.max_strengths[arfcn] = value
                    self.gsmwsdb.execute("UPDATE MAX_STRENGTHS SET TIMESTAMP=?, RSSI=? WHERE ARFCN=?", (now, value, arfcn))

            to_delete = []
            for arfcn in self.max_strengths:
                if arfcn not in strengths:
                    to_delete.append(arfcn)
                    self.gsmwsdb.execute("DELETE FROM MAX_STRENGTHS WHERE ARFCN=?", (arfcn,))
            for arfcn in to_delete:
                del self.max_strengths[arfcn]
            self.gsmwsdb.commit()



    def update_recent_strengths(self, strengths):
        for arfcn in strengths:
            value = strengths[arfcn]
            if arfcn in self.recent_strengths:
                self.recent_strengths[arfcn].append(value)
            else:
                self.recent_strengths[arfcn] = collections.deque([value],maxlen=self.strengths_maxlen)

        with self.gsmwsdb_lock:
            to_delete = []
            for arfcn in self.recent_strengths:
                if arfcn not in strengths:
                    to_delete.append(arfcn)
                    self.gsmwsdb.execute("DELETE FROM AVG_STRENGTHS WHERE ARFCN=?", (arfcn,))

            for arfcn in to_delete:
                del self.recent_strengths[arfcn]

        # force a write whenever we update strength
        self.rssi()
        self.__write_rssi()



    def process(self, message):
        logging.info("In Decoder process")
        self.msgs_seen += 1
        if message.startswith("GSM A-I/F DTAP - Measurement Report"):
            logging.info("In Decoder Measurement Report")
            if self.ignore_reports or self.current_arfcn is None or len(self.last_arfcns) == 0:
                return # skip for now, we don't have enough data to work with

            report = gsm.MeasurementReport(self.last_arfcns, self.current_arfcn, message)
            if report.valid:
                logging.info("(decoder %d) MeasurementReport: " % (self.decoder_id) + str(report))
                self.reports.put(report.current_strengths)
             # removed the for loop from here
                self.update_max_strength(report.current_strengths)
                self.update_recent_strengths(report.current_strengths)

                for arfcn in report.current_bsics:
                    if report.current_bsics[arfcn] != None:
                        logging.debug("ZOUNDS! AN ENEMY BSIC: %d (ARFCN %d, decoder %d)" % (report.current_bsics[arfcn], arfcn, self.decoder_id))
                        
            #gsmtap = gsm.GSMTAP(message)
            #neighbor_details = report.neighbor_details
            #if self.runtime["initial_time"] == None:
            #    self.runtime["initial_time"] = datetime.datetime.now()
            #timestamp = datetime.datetime.now()
            #indexes = []

            #if len(neighbor_details["arfcns"]) > 0:

            #    for arfcn in neighbor_details["arfcns"]:
                        #logging.info("(decoder %d) MeasureMent Report: Neighbor ARFCN=%s" % (self.decoder_id, arfcn))
                        #neighbor_details["arfcns"][arfcn]
            #            if str(arfcn) != 0:
            #                if arfcn not in self.runtime["arfcns"]:
            #                    self.runtime["arfcns"].append(arfcn)#neighbor_details["arfcns"][arfcn])
                                #self.runtime["rssis"].append(neighbor_details["rssis"][arfcn])
            #                    self.runtime["arfcn_tracking"].insert(neighbor_details["arfcns"].index(arfcn), True)#neighbor_details["arfcns"][arfcn]), True)
                           
            #                else:
            #                    self.runtime["arfcn_tracking"].insert(neighbor_details["arfcns"].index(arfcn), True)#neighbor_details["arfcns"][arfcn]), True)
                        
            #                indexes.append(neighbor_details["arfcns"].index(arfcn))

            #    for rssi in neighbor_details["rssis"]:
            #        if rssi not in self.runtime["rssis"]:
            #           self.runtime["rssis"].append(rssi) 
  
            #    for _ in self.runtime["arfcn_tracking"]:
            #        if _ not in indexes:
            #            self.runtime["arfcn_tracking"].insert(self.runtime["arfcn_tracking"].index(_), False)

            #checked_time = timestamp - self.runtime["initial_time"]
            #if checked_time.seconds > self.NEIGHBOR_CYCLE_TIME:
            #    if len(self.runtime["arfcns"]) > 0:
                    # unique_list_of_arfcns = list(set(self.runtime["arfcns"]))
            #        with self.gsmwsdb_lock:
            #            for tracker in self.runtime["arfcn_tracking"]:
            #                if tracker is False:
            #                    self.gsmwsdb.execute("INSERT INTO AVAIL_ARFCN VALUES(?,?,?)",
            #                                     (tracker, timestamp, self.runtime["rssis"][self.runtime["arfcn_tracking"].index(tracker)]))
        elif message.startswith("GSM CCCH - System Information Type 2"):
            sysinfo2 = gsm.SystemInformationTwo(message)
            self.last_arfcns = sysinfo2.arfcns
            self.ncc_permitted = sysinfo2.ncc_permitted
            logging.debug("(decoder %d) SystemInformation2: %s" % (self.decoder_id, str(sysinfo2.arfcns)))
        elif message.startswith("GSM TAP Header"):
            gsmtap = gsm.GSMTAP(message)
            self.current_arfcn = gsmtap.arfcn
            logging.debug("(decoder %d) GSMTAP: Current ARFCN=%s" % (self.decoder_id, str(gsmtap.arfcn)))

