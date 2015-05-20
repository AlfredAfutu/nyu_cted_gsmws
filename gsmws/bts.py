"""
This file is part of GSMWS.
"""

import datetime
import sqlite3
import logging
import threading
import envoy
import openbts
import random
import decoder

class BTS(object):
    """
    Provides access to handover and power related settings on a single, local
    OpenBTS instance.
    """
    def __init__(self, loglvl=logging.DEBUG):
        self.node_manager = openbts.OpenBTS()
        self.cmd_socket = ('/var/run/command');

        neighbor_table_loc = (self.node_manager
                                .read_config("Peering.NeighborTable.Path")
                                .data['value'])

        self.neighbor_table = sqlite3.connect(neighbor_table_loc)
        self.neighbors = []
        self.loglvl = loglvl

        self.decoder = None;
        self.gsmwsdb_lock = threading.Lock()
        #self.decoder = decoder.GSMDecoder()
        #self.decoder.daemon = True
        #self.decoder.start()

    def init_decoder(self, gsm_decoder):
        """ Start the decoder for this BTS. We do this separately since we want
        to be able to create the BTS external to the controller, but we don't
        have a global DB lock until the controller starts."""
        logging.info("Decoder started")
        self.decoder = gsm_decoder
        self.decoder.start()

    def is_off(self):
        """
        We define the BTS as off if it's in txatten is > 90
        """
        txatten = int(self.node_manager
                    .read_config('TRX.TxAttenOffset').data['value'])
        return txatten > 90

    def current_arfcn(self):
        """
        Check for the current ARFCN in use, according to OpenBTS.
        """
        return int(self.node_manager.read_config("GSM.Radio.C0").data['value'])

    def reports(self):
        """
        Gets all the reports from the decoder.
        """
        return self.decoder.reports.getall()

    def offset_correct(self):
        """ We need to make sure the offset for the radio is set correctly,
        else handover will fail. This shows up as phones not sending measurement
        reports for both ARFCNs, not reselecting, etc. """

        # this works because the "default" offset is defined by the setting in
        # the radio's firmware; if the value in the DB is different from the
        # offset, it won't be set to default.
        offset = self.node_manager.read_config("TRX.RadioFrequencyOffset")
        return offset['defaultValue'] == offset['value']


    def command(self, command_str):
        """ Run a command as though we're using the OpenBTSCLI.

        Returns:
            Output of the command if successful
            Raises a ValueError if failure (probably*)

        * We say probably because there's no good way to know if a command sent
        through OpenBTSDo succeeds or not! WHY WOULD YOU NEED THAT.
        """

        # THIS IS THE OFFICIAL WAY TO DO THIS
        # IN THE NAME OF ALL THAT IS HOLY
        r = envoy.run("echo '%s' | sudo /OpenBTS/OpenBTSDo %s"
                        % (command_str, self.cmd_socket))

        # More fun: always exits with status 0! So we have to check contents of
        # stdout for "known" error messages from OpenBTS (CLI/CLI.cpp) to guess
        # if the command succeeded or failed. These are not guaranteed to be in
        # output, so just a guess.
        failure_messages = ["wrong number of arguments",
                            "bad argument(s)",
                            "command not found",
                            "too many arguments for parser",
                            "command failed"]
        response = r.std_out.strip()
        for msg in failure_messages:
            if msg in response:
                raise ValueError("%s: %s" % (msg, response))
        return response


    def restart(self):
        """
        Restarts the BTS. Note that OpenBTS must be running as a supervisorctl
        job.
        """
        logging.warning("Restarting openbts")
        envoy.run("sudo supervisordctl restart openbts")


    def set_txatten(self, value):
        """ Sets the txatten value. Takes effect immediately.

        Args:
            value: attenuation in dB w.r.t. full power (100mW = 20dBm)

        Returns:
            response to command, or raises ValueError if invalid setting

        """
        self.command("txatten %d" % (value))


    def change_arfcn(self, new_arfcn, immediate=False):
        """ Change OpenBTS to use a new ARFCN. By default, just update the DB, but
        don't actually restart OpenBTS. If immediate=True, restart OpenBTS too. """
        try:
            self.node_manager.update_config("GSM.Radio.C0", new_arfcn)
        except openbts.exceptions.InvalidRequestError:
            return False
        logging.warning("Updated ARFCN to %s" % new_arfcn)
        if immediate:
            # this is a blocking call
            self.restart()
        return True

    def get_random_c0s(self, gsmws_db):
        logging.info("In get random c0s")
        chosen_c0s = []
        random_c0s = random.sample(xrange(1, 124), 5)
        logging.info("Random C0s %s" % random_c0s)
        with self.gsmwsdb_lock:
            available_arfcns = (gsmws_db.execute("SELECT ARFCN FROM AVAIL_ARFCN").fetchall())

            if len(available_arfcns) == 0:
                logging.info("No available_arfcns yet")
                for c0 in random_c0s:
                    chosen_c0s.append(c0)
            else:
                logging.info("Available_arfcns present")
                for c0 in random_c0s:
                    if c0 not in available_arfcns:
                        chosen_c0s.append(c0)
                    else:
                        c0_timestamp = (gsmws_db.execute("SELECT TIMESTAMP FROM AVAIL_ARFCN WHERE ARFCN=?", c0))
                        time_difference = (datetime.datetime.now - c0_timestamp)
                        hour_difference = time_difference.seconds / 60 / 60
                        logging.info("Hour difference %s" % hour_difference)
                        if hour_difference > 24:
                            chosen_c0s.append(c0) 

       
        return chosen_c0s
      

    def put_c0s_into_file(self, gsmws_db):
        logging.info("In Put c0s into file")
        c0s_for_file =self.get_random_c0s(gsmws_db)
        if len(c0s_for_file) == 0:
            logging.info("C0s for file is empty")
            self.put_c0s_into_file(gsmws_db)
        else:
            logging.info("Putting C0s in file")
            with open('/var/run/c0file.txt', 'w+') as c0file:
                c0file.truncate()
                for c0 in c0s_for_file:
                    c0file.write("%d\n" % c0)


    def set_neighbors(self, arfcns, gsmws_db, real=[]):
        """
        The new OpenBTS handover feature makes setting the neighbor list a bit
        more complicated. You're supposed to just set the IP addresses of the
        neighbor cells, and then OpenBTS populates a neighbor table DB
        (/var/run/NeighborTable.db by default, set in Peering.NeighborTable.Path)
        with ARFCN, BSIC, etc by directly querying the other BTS. Manually
        populating this DB requires two steps. First, we have to add IP addresses
        to GSM.Neighbors; if we don't, anything we add to the neighbor table will
        ue deleted. Once we've added an IP, we can manually update the
        NeighborTable with our list of neighbor ARFCNs.

        Our approach is to use unrouteable 127.0.10.0/24 addresses for our
        neighbors; we simply incrementally add neighbor IPs based on how many
        ARFCNs we want to scan. Once we've done that, we can directly
        manipulate the neighbor table using those IP addresses, thereby setting
        arbitrary ARFCNs as our neighbors. This is potentially problematic if
        you're running multiple instances of OpenBTS on the same host.

        If we have a real BTS as a neighbor, we add those here too.

        Args:
            arfcns: List of ARFCNs to scan
            real:   List of real BTS neighbor IP addresses

        Returns:
            True if we successfully set up the new neighbors, false otherwise
        """
          
        # set GSM.Neighbors to empty string 
        try:
            logging.info("In setting GSM.Neighbors to empty string")
            logging.info("Gsmws db conneciton % s" % gsmws_db)
            r = self.node_manager.update_config("GSM.Neighbors", "")
           # logging.debug("Updating neighbors (%s) '%s': '%s'" % (arfcns, neighbor_string, r.data))
            logging.info("Updating neighbors with empty string %s'" % r.data)
        except openbts.exceptions.InvalidResponseError:
            logging.debug("neighbors unchanged")
            logging.info("neighbors unchanged")

        # put random c0s into file
        logging.info("About to enter put c0s into file")
        self.put_c0s_into_file(gsmws_db)

        # Need to generate a mapping of ARFCNs : IPs
        logging.info("About to put fake IPs in GSM.Neighbors")
        fake_neighbors = {}
        # set 5 IPs  len(arfcns)
        for i in range(0, 5):
            chan = arfcns[i]
            fake_neighbors[chan] = "127.0.0.%d:16001" % (i + 10,)

        real_ip_str = " ".join([str(bts_ip) for bts_ip in real])
        fake_ip_str = " ".join([str(ip) for ip in fake_neighbors.values()])

        self.neighbors = arfcns

        # set IPs in openbts
        # leading space will choke OpenBTS
        neighbor_string = ("%s %s" % (real_ip_str, fake_ip_str)).strip()
        try:
            r = self.node_manager.update_config("GSM.Neighbors", neighbor_string)
            logging.debug("Updating neighbors (%s) '%s': '%s'" % (arfcns, neighbor_string, r.data))
            logging.info("Updating neighbors (%s) '%s': '%s'" % (arfcns, neighbor_string, r.data))
        except openbts.exceptions.InvalidResponseError:
            # OpenBTS won't accept the same list of neighbor IPs twice, so this
            # will come up every time we set the same number of ARFCNs to scan.
            # Totally normal.
            logging.debug("neighbors unchanged")
            logging.info("neighbors unchanged")


        # Update the neighbor table for each fake neighbor. Real neighbors
        # should be updated automatically on their own.
        #
        # IP: one of our fake IPs
        # Updated: Time when we updated the neighbor
        # Holdoff: GSM.Handover.FailureHoldoff, time in seconds to wait before
        #          attempting another handover with this neighbor after failure.
        # C0: The ARFCN we want to scan
        # BSIC: The BSIC. Can be set to whatever?
        updated = int(datetime.datetime.now().strftime("%s"))
        holdoff = 3600*24*7 # 7 days
        bsic = 1 # TODO does this matter?
        """
        try:
            delete_query_str = "DELETE FROM NEIGHBOR_TABLE WHERE 1;"
            self.neighbor_table.execute(delete_query_str)
            for arfcn, ip in fake_neighbors.iteritems():
               # query_str = "UPDATE NEIGHBOR_TABLE SET C0 = ?, UPDATED = ?, HOLDOFF = ?, BSIC = ? WHERE IPADDRESS = ?;"
               # delete_query_str = "DELETE FROM NEIGHBOR_TABLE WHERE 1;"
               # self.neighbor_table.execute(delete_query_str)
                insert_query_str = "INSERT INTO NEIGHBOR_TABLE VALUES(?,?,?,?,?);"
               # self.neighbor_table.execute(query_str, (arfcn, updated, holdoff, bsic, ip))
                self.neighbor_table.execute(insert_query_str, (ip, updated, holdoff, arfcn, bsic))
            self.neighbor_table.commit()
            logging.info("Updated NeighborTable.")
            return True
        except sqlite3.OperationalError as operationalError:
            logging.info("SQlite Operational Error is : '%s'" % operationalError)
            logging.info("Could not update NeighborTable.")
            return False
        """
