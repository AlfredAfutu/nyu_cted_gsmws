"""
This file is part of GSMWS.
"""

import subprocess
import sys
import datetime
import re
import logging

"""
Rather than decoding the actual packet stream, we just run tshark w/ verbose
output and parse the output.

Things we care about:
    - Measurement report history, e.g., an ARFCN->RSSI mapping for every
      measurement report.
    - Current ARFCNs we're broadcasting ("LIST OF ARFCNs")
    - Current serving cell
    - Current serving cell strength

We continually run tshark in a separate process, and parse its output. We
maintain a timestamped history of all measurement reports and current ARFCNs as
well as a moving average of the RSSI for each.
"""

regex = {'current_strength': re.compile("RXLEV-FULL-SERVING-CELL:.*dBm \((\d+)\)"),
         'num_cells': re.compile("NO-NCELL-M:.*result \((\d+)\)"),
         'cell_report': re.compile("RXLEV-NCELL: (\d+)\n.*= BCCH-FREQ-NCELL: (\d+)\n.* = BSIC-NCELL: (\d)"),
         'arfcn': re.compile("GSM TAP Header, ARFCN: (\d+)"),
         'sys_info_2': re.compile("List of ARFCNs =([ \d]+).*(\d{4} \d{4}) = NCC Permitted",re.DOTALL),
         }
def command_stream(command):
    cmd_list = command.split()
    proc = subprocess.Popen(cmd_list, stdout=subprocess.PIPE)
    return proc.stdout

class MeasurementReport(object):
    def __init__(self, last_arfcns, current_arfcn, result_msg):
        self.timestamp = datetime.datetime.now()
        self.result_msg = result_msg
        self.valid = False
        self.current_strengths, self.current_bsics = self.parse(last_arfcns, current_arfcn)

    @staticmethod
    def sample():
        return """
GSM A-I/F DTAP - Measurement Report
    Protocol Discriminator: Radio Resources Management messages
        .... 0110 = Protocol discriminator: Radio Resources Management messages (0x06)
        0000 .... = Skip Indicator: 0
    DTAP Radio Resources Management Message Type: Measurement Report (0x15)
    Measurement Results
        0... .... = BA-USED: 0
        .0.. .... = DTX-USED: DTX was not used
        ..01 0000 = RXLEV-FULL-SERVING-CELL: -95 <= x < -94 dBm (16)
        0... .... = 3G-BA-USED: 0
        .0.. .... = MEAS-VALID: The measurement results are valid
        RXLEV-SUB-SERVING-CELL: -95 <= x < -94 dBm (16)
        .111 .... = RXQUAL-FULL-SERVING-CELL: BER > 12.8%, Mean value 18.10% (7)
        .... 111. = RXQUAL-SUB-SERVING-CELL: BER > 12.8%, Mean value 18.10% (7)
        .... ...0  01.. .... = NO-NCELL-M: 1 neighbour cell measurement result (1)
        ..01 0001 = RXLEV-NCELL: 17
        0001 0... = BCCH-FREQ-NCELL: 2
        .... .000  010. .... = BSIC-NCELL: 2"""

    def parse(self, last_arfcns, current_arfcn, result_msg=None):
        if result_msg == None:
            result_msg = self.result_msg
        strengths = dict(zip(last_arfcns, [-0.001 for _ in range(0, len(last_arfcns))]))
        bsics = dict(zip(last_arfcns, [None for _ in range(0, len(last_arfcns))]))
        serving_strength = int(regex['current_strength'].findall(result_msg)[0])
        strengths[current_arfcn] = serving_strength

        try:
            num_cells = int(regex['num_cells'].findall(result_msg)[0])
        except IndexError:
            return {}, {}

        neighbor_reports = regex['cell_report'].findall(result_msg)
        #print neighbor_reports

        assert len(neighbor_reports) == num_cells
        for report in neighbor_reports:
            #print last_arfcns[int(report[1])]
            #print int(report[0])
            strengths[last_arfcns[int(report[1])]] = int(report[0])
            if not current_arfcn == last_arfcns[int(report[1])]:
                # TODO: ignore current arfcn bsic for now. This could be a good
                # way to detect same-channel interference w/ a single ARFCN: if
                # the measurement report doesn't match our current BSIC then we
                # can safely assume we're seeing another tower!
                bsics[last_arfcns[int(report[1])]] = int(report[2])

        self.valid = True
        return strengths, bsics



    def __str__(self):
        return "%s %s" % (self.timestamp, str(self.current_strengths))


class GSMTAP(object):
    def __init__(self, message):
        self.timestamp = datetime.datetime.now()
        self.message = message
        self.arfcn = self.parse()
        self.neighbor_details = self.get_arfcns()

    def parse(self, message=None):
        if message == None:
            message = self.message
        return int(regex['arfcn'].findall(message)[0])

    def get_arfcns(self, message=None):
        if message == None:
            message = self.message
        neighbors_dict = {}
        neighbors_dict["arfcns"] = []
        neighbors_dict["rssis"] = []
        neighbor_reports = regex['cell_report'].findall(message)
        #assert len(neighbor_reports) == int(regex['num_cells'].findall(message)[0])
        logging.info("Neighbor report size %s" % len(neighbor_reports))
        logging.info("Number of cells %s" % (regex['num_cells'].findall(message)))
        for report in neighbor_reports:
            neighbors_dict["arfcns"].insert(report, int(report[1]))
            neighbors_dict["rssis"].insert(report, int(report[0]))

        return neighbors_dict


class SystemInformationTwo(object):
    def __init__(self, message):
        self.timestamp = datetime.datetime.now()
        self.message = message
        self.arfcns, self.ncc_permitted = self.parse()

    @staticmethod
    def sample():
        return """
L2 Pseudo Length
    0101 10.. = L2 Pseudo Length value: 22
Protocol Discriminator: Radio Resources Management messages
    .... 0110 = Protocol discriminator: Radio Resources Management messages (0x06)
    0000 .... = Skip Indicator: 0
Message Type: System Information Type 2
Neighbour Cell Description - BCCH Frequency List
    ..0. .... = EXT-IND: The information element carries the complete BA (0)
    ...0 .... = BA-IND: 0
    10.. 111. = Format Identifier: variable bit map (0x47)
List of ARFCNs = 23 33 51 59 99
NCC Permitted
    1111 1111 = NCC Permitted: 0xff
RACH Control Parameters
    01.. .... = Max retrans: Maximum 2 retransmissions (1)
    ..11 10.. = Tx-integer: 32 slots used to spread transmission (14)
    .... ..0. = CELL_BARR_ACCESS: The cell is not barred (0)
    .... ...1 = RE: True
    0000 0000 0000 0000 = ACC: 0x0000
        """

    def parse(self, message=None):
        if message == None:
            message = self.message

        res = regex['sys_info_2'].findall(message)[0]
        arfcns = map(int,res[0].split())
        ncc_permitted = res[1]
        return arfcns, ncc_permitted
