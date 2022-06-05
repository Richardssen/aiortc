import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import attr
from aioice import Candidate, Connection
from pyee import AsyncIOEventEmitter

from .exceptions import InvalidStateError
from .rtcconfiguration import RTCIceServer

STUN_REGEX = re.compile(
    r"(?P<scheme>stun|stuns)\:(?P<host>[^?:]+)(\:(?P<port>[0-9]+?))?"
)
TURN_REGEX = re.compile(
    r"(?P<scheme>turn|turns)\:(?P<host>[^?:]+)(\:(?P<port>[0-9]+?))?"
    r"(\?transport=(?P<transport>.*))?"
)

logger = logging.getLogger("ice")


@attr.s
class RTCIceCandidate:
    """
    The :class:`RTCIceCandidate` interface represents a candidate Interactive
    Connectivity Establishment (ICE) configuration which may be used to
    establish an RTCPeerConnection.
    """

    component = attr.ib()  # type: int
    foundation = attr.ib()  # type: str
    ip = attr.ib()  # type: str
    port = attr.ib()  # type: int
    priority = attr.ib()  # type: int
    protocol = attr.ib()  # type: str
    type = attr.ib()
    relatedAddress = attr.ib(default=None)
    relatedPort = attr.ib(default=None)
    sdpMid = attr.ib(default=None)
    sdpMLineIndex = attr.ib(default=None)
    tcpType = attr.ib(default=None)


@attr.s
class RTCIceParameters:
    """
    The :class:`RTCIceParameters` dictionary includes the ICE username
    fragment and password and other ICE-related parameters.
    """

    usernameFragment = attr.ib(default=None)  # type: Optional[str]
    "ICE username fragment."

    password = attr.ib(default=None)  # type: Optional[str]
    "ICE password."

    iceLite = attr.ib(default=False)  # type: bool


def candidate_from_aioice(x: Candidate) -> RTCIceCandidate:
    return RTCIceCandidate(
        component=x.component,
        foundation=x.foundation,
        ip=x.host,
        port=x.port,
        priority=x.priority,
        protocol=x.transport,
        relatedAddress=x.related_address,
        relatedPort=x.related_port,
        tcpType=x.tcptype,
        type=x.type,
    )


def candidate_to_aioice(x: RTCIceCandidate) -> Candidate:
    return Candidate(
        component=x.component,
        foundation=x.foundation,
        host=x.ip,
        port=x.port,
        priority=x.priority,
        related_address=x.relatedAddress,
        related_port=x.relatedPort,
        transport=x.protocol,
        tcptype=x.tcpType,
        type=x.type,
    )


def connection_kwargs(servers: List[RTCIceServer]) -> Dict[str, Any]:
    kwargs = {}  # type: Dict[str, Any]

    for server in servers:
        uris = server.urls if isinstance(server.urls, list) else [server.urls]
        for uri in uris:
            parsed = parse_stun_turn_uri(uri)

            if parsed["scheme"] == "stun":
                # only a single STUN server is supported
                if "stun_server" in kwargs:
                    continue

                kwargs["stun_server"] = (parsed["host"], parsed["port"])
            elif parsed["scheme"] in ["turn", "turns"]:
                # only a single TURN server is supported
                if "turn_server" in kwargs:
                    continue

                # only 'udp' and 'tcp' transports are supported
                if parsed["scheme"] == "turn" and parsed["transport"] not in [
                    "udp",
                    "tcp",
                ]:
                    continue
                elif parsed["scheme"] == "turns" and parsed["transport"] != "tcp":
                    continue

                # only 'password' credentialType is supported
                if server.credentialType != "password":
                    continue

                kwargs["turn_server"] = (parsed["host"], parsed["port"])
                kwargs["turn_ssl"] = parsed["scheme"] == "turns"
                kwargs["turn_transport"] = parsed["transport"]
                kwargs["turn_username"] = server.username
                kwargs["turn_password"] = server.credential

    return kwargs


def parse_stun_turn_uri(uri: str) -> Dict[str, Any]:
    if uri.startswith("stun"):
        match = STUN_REGEX.fullmatch(uri)
    elif uri.startswith("turn"):
        match = TURN_REGEX.fullmatch(uri)
    else:
        raise ValueError("malformed uri: invalid scheme")

    if not match:
        raise ValueError("malformed uri")

    # set port
    parsed = match.groupdict()
    if parsed["port"]:
        parsed["port"] = int(parsed["port"])
    elif parsed["scheme"] in ["stuns", "turns"]:
        parsed["port"] = 5349
    else:
        parsed["port"] = 3478

    # set transport
    if parsed["scheme"] == "turn" and not parsed["transport"]:
        parsed["transport"] = "udp"
    elif parsed["scheme"] == "turns" and not parsed["transport"]:
        parsed["transport"] = "tcp"

    return parsed


class RTCIceGatherer(AsyncIOEventEmitter):
    """
    The :class:`RTCIceGatherer` interface gathers local host, server reflexive
    and relay candidates, as well as enabling the retrieval of local
    Interactive Connectivity Establishment (ICE) parameters which can be
    exchanged in signaling.
    """

    def __init__(self, iceServers: Optional[List[RTCIceServer]] = None) -> None:
        super().__init__()

        if iceServers is None:
            iceServers = self.getDefaultIceServers()
        ice_kwargs = connection_kwargs(iceServers)

        self._connection = Connection(ice_controlling=False, **ice_kwargs)
        self.__state = "new"

    @property
    def state(self) -> str:
        """
        The current state of the ICE gatherer.
        """
        return self.__state

    async def gather(self) -> None:
        """
        Gather ICE candidates.
        """
        if self.__state == "new":
            self.__setState("gathering")
            await self._connection.gather_candidates()
            self.__setState("completed")

    @classmethod
    def getDefaultIceServers(cls) -> List[RTCIceServer]:
        """
        Return the list of default :class:`RTCIceServer`.
        """
        return [RTCIceServer("stun:stun.l.google.com:19302")]

    def getLocalCandidates(self) -> List[RTCIceCandidate]:
        """
        Retrieve the list of valid local candidates associated with the ICE
        gatherer.
        """
        return [candidate_from_aioice(x) for x in self._connection.local_candidates]

    def getLocalParameters(self) -> RTCIceParameters:
        """
        Retrieve the ICE parameters of the ICE gatherer.

        :rtype: RTCIceParameters
        """
        return RTCIceParameters(
            usernameFragment=self._connection.local_username,
            password=self._connection.local_password,
        )

    def __setState(self, state: str) -> None:
        self.__state = state
        self.emit("statechange")


class RTCIceTransport(AsyncIOEventEmitter):
    """
    The :class:`RTCIceTransport` interface allows an application access to
    information about the Interactive Connectivity Establishment (ICE)
    transport over which packets are sent and received.

    :param gatherer: An :class:`RTCIceGatherer`.
    """

    def __init__(self, gatherer: RTCIceGatherer) -> None:
        super().__init__()
        self.__start = None  # type: Optional[asyncio.Event]
        self.__iceGatherer = gatherer
        self.__state = "new"
        self._connection = gatherer._connection

    @property
    def iceGatherer(self) -> RTCIceGatherer:
        """
        The ICE gatherer passed in the constructor.
        """
        return self.__iceGatherer

    @property
    def role(self) -> str:
        """
        The current role of the ICE transport.

        Either `'controlling'` or `'controlled'`.
        """
        return "controlling" if self._connection.ice_controlling else "controlled"

    @property
    def state(self) -> str:
        """
        The current state of the ICE transport.
        """
        return self.__state

    def addRemoteCandidate(self, candidate: Optional[RTCIceCandidate]) -> None:
        """
        Add a remote candidate.

        :param candidate: The new candidate or `None` to signal end of candidates.
        """
        # FIXME: don't use private member!
        if not self._connection._remote_candidates_end:
            if candidate is None:
                self._connection.add_remote_candidate(None)
            else:
                self._connection.add_remote_candidate(candidate_to_aioice(candidate))

    def getRemoteCandidates(self):
        """
        Retrieve the list of candidates associated with the remote
        :class:`RTCIceTransport`.
        """
        return [candidate_from_aioice(x) for x in self._connection.remote_candidates]

    async def start(self, remoteParameters: RTCIceParameters) -> None:
        """
        Initiate connectivity checks.

        :param remoteParameters: The :class:`RTCIceParameters` associated with
                                  the remote :class:`RTCIceTransport`.
        """
        if self.state == "closed":
            raise InvalidStateError("RTCIceTransport is closed")

        # handle the case where start is already in progress
        if self.__start is not None:
            await self.__start.wait()
            return
        self.__start = asyncio.Event()

        self.__setState("checking")
        self._connection.remote_username = remoteParameters.usernameFragment
        self._connection.remote_password = remoteParameters.password
        try:
            await self._connection.connect()
        except ConnectionError:
            self.__setState("failed")
        else:
            self.__setState("completed")
        self.__start.set()

    async def stop(self) -> None:
        """
        Irreversibly stop the :class:`RTCIceTransport`.
        """
        if self.state != "closed":
            self.__setState("closed")
            await self._connection.close()

    async def _recv(self) -> bytes:
        try:
            return await self._connection.recv()
        except ConnectionError:
            if self.state == "completed":
                self.__setState("failed")
            raise

    async def _send(self, data: bytes) -> None:
        try:
            await self._connection.send(data)
        except ConnectionError:
            if self.state == "completed":
                self.__setState("failed")
            raise

    def __log_debug(self, msg: str, *args) -> None:
        logger.debug(f"{self.role} {msg}", *args)

    def __setState(self, state: str) -> None:
        if state != self.__state:
            self.__log_debug("- %s -> %s", self.__state, state)
            self.__state = state
            self.emit("statechange")

            # no more events will be emitted, so remove all event listeners
            # to facilitate garbage collection.
            if state == "closed":
                self.iceGatherer.remove_all_listeners()
                self.remove_all_listeners()
