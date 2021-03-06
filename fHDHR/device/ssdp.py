# Adapted from https://github.com/MoshiBin/ssdpy and https://github.com/ZeWaren/python-upnp-ssdp-example
import socket
import struct


class fHDHR_Detect():

    def __init__(self, fhdhr):
        self.fhdhr = fhdhr
        self.fhdhr.db.delete_fhdhr_value("ssdp_detect", "list")

    def set(self, location):
        detect_list = self.fhdhr.db.get_fhdhr_value("ssdp_detect", "list") or []
        if location not in detect_list:
            detect_list.append(location)
            self.fhdhr.db.set_fhdhr_value("ssdp_detect", "list", detect_list)

    def get(self):
        return self.fhdhr.db.get_fhdhr_value("ssdp_detect", "list") or []


class SSDPServer():

    def __init__(self, fhdhr):
        self.fhdhr = fhdhr

        self.detect_method = fHDHR_Detect(fhdhr)

        if fhdhr.config.dict["fhdhr"]["discovery_address"]:

            self.sock = None
            self.proto = "ipv4"
            self.port = 1900
            self.iface = None
            self.address = None
            self.server = 'fHDHR/%s UPnP/1.0' % fhdhr.version

            allowed_protos = ("ipv4", "ipv6")
            if self.proto not in allowed_protos:
                raise ValueError("Invalid proto - expected one of {}".format(allowed_protos))

            self.nt = 'urn:schemas-upnp-org:device:MediaServer:1'
            self.usn = 'uuid:' + fhdhr.config.dict["main"]["uuid"] + '::' + self.nt
            self.location = ('http://' + fhdhr.config.dict["fhdhr"]["discovery_address"] + ':' +
                             str(fhdhr.config.dict["fhdhr"]["port"]) + '/device.xml')
            self.al = self.location
            self.max_age = 1800
            self._iface = None

            if self.proto == "ipv4":
                self._af_type = socket.AF_INET
                self._broadcast_ip = "239.255.255.250"
                self._address = (self._broadcast_ip, self.port)
                self.bind_address = "0.0.0.0"
            elif self.proto == "ipv6":
                self._af_type = socket.AF_INET6
                self._broadcast_ip = "ff02::c"
                self._address = (self._broadcast_ip, self.port, 0, 0)
                self.bind_address = "::"

            self.broadcast_addy = "{}:{}".format(self._broadcast_ip, self.port)

            self.sock = socket.socket(self._af_type, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Bind to specific interface
            if self.iface is not None:
                self.sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), self.iface)

            # Subscribe to multicast address
            if self.proto == "ipv4":
                mreq = socket.inet_aton(self._broadcast_ip)
                if self.address is not None:
                    mreq += socket.inet_aton(self.address)
                else:
                    mreq += struct.pack(b"@I", socket.INADDR_ANY)
                self.sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq,
                )
                # Allow multicasts on loopback devices (necessary for testing)
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            elif self.proto == "ipv6":
                # In IPv6 we use the interface index, not the address when subscribing to the group
                mreq = socket.inet_pton(socket.AF_INET6, self._broadcast_ip)
                if self.iface is not None:
                    iface_index = socket.if_nametoindex(self.iface)
                    # Send outgoing packets from the same interface
                    self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, iface_index)
                    mreq += struct.pack(b"@I", iface_index)
                else:
                    mreq += socket.inet_pton(socket.AF_INET6, "::")
                self.sock.setsockopt(
                    socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq,
                )
                self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 1)
            self.sock.bind((self.bind_address, self.port))

            self.notify_payload = self.create_notify_payload()
            self.msearch_payload = self.create_msearch_payload()

            self.m_search()

    def on_recv(self, data, address):
        self.fhdhr.logger.debug("Received packet from {}: {}".format(address, data))

        (host, port) = address

        try:
            header, payload = data.decode().split('\r\n\r\n')[:2]
        except ValueError:
            self.fhdhr.logger.error("Error with Received packet from {}: {}".format(address, data))
            return

        lines = header.split('\r\n')
        cmd = lines[0].split(' ')
        lines = map(lambda x: x.replace(': ', ':', 1), lines[1:])
        lines = filter(lambda x: len(x) > 0, lines)

        headers = [x.split(':', 1) for x in lines]
        headers = dict(map(lambda x: (x[0].lower(), x[1]), headers))

        if cmd[0] == 'M-SEARCH' and cmd[1] == '*':
            # SSDP discovery
            self.fhdhr.logger.debug("Received qualifying M-SEARCH from {}".format(address))
            self.fhdhr.logger.debug("M-SEARCH data: {}".format(headers))
            notify = self.notify_payload
            self.fhdhr.logger.debug("Created NOTIFY: {}".format(notify))
            try:
                self.sock.sendto(notify, address)
            except OSError as e:
                # Most commonly: We received a multicast from an IP not in our subnet
                self.fhdhr.logger.debug("Unable to send NOTIFY to {}: {}".format(address, e))
                pass
        elif cmd[0] == 'NOTIFY' and cmd[1] == '*':
            # SSDP presence
            self.fhdhr.logger.debug("NOTIFY data: {}".format(headers))
            try:
                if headers["server"].startswith("fHDHR"):
                    if headers["location"] != self.location:
                        self.detect_method.set(headers["location"].split("/device.xml")[0])
            except KeyError:
                return
        else:
            self.fhdhr.logger.debug('Unknown SSDP command %s %s' % (cmd[0], cmd[1]))

    def m_search(self):
        data = self.msearch_payload
        self.sock.sendto(data, self._address)

    def create_notify_payload(self):
        if self.max_age is not None and not isinstance(self.max_age, int):
            raise ValueError("max_age must by of type: int")
        data = (
            "NOTIFY * HTTP/1.1\r\n"
            "HOST:{}\r\n"
            "NT:{}\r\n"
            "NTS:ssdp:alive\r\n"
            "USN:{}\r\n"
            "SERVER:{}\r\n"
        ).format(
                 self._broadcast_ip,
                 self.nt,
                 self.usn,
                 self.server
                 )
        if self.location is not None:
            data += "LOCATION:{}\r\n".format(self.location)
        if self.al is not None:
            data += "AL:{}\r\n".format(self.al)
        if self.max_age is not None:
            data += "Cache-Control:max-age={}\r\n".format(self.max_age)
        data += "\r\n"
        return data.encode("utf-8")

    def create_msearch_payload(self):
        data = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST:{}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "ST:{}\r\n"
            "MX:{}\r\n"
        ).format(
                 self.broadcast_addy,
                 "ssdp:all",
                 1
                 )
        data += "\r\n"
        return data.encode("utf-8")

    def run(self):
        try:
            while True:
                data, address = self.sock.recvfrom(1024)
                self.on_recv(data, address)
        except KeyboardInterrupt:
            self.sock.close()
