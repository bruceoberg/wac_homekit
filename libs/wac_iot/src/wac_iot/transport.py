#!/usr/bin/env python3
"""The only module that knows about HTTP.

Everything else in this package composes request bodies and reads response
objects; this is where a body becomes a socket write. Also home to
`ProbeHost`, which answers the question the vendor documentation does not:
whether a given device actually listens on plain HTTP, on TLS, or both.
"""

from __future__ import annotations  # Forward refs without quotes

import asyncio
import hashlib
import json
import logging
import ssl

from typing import Any

import aiohttp

from pydantic import Field

from .errors import (
	ErrFromResponse,
	ErrFromStatus,
	WacError,
	WacResponseError,
	WacTimeoutError,
	WacTransportError,
)
from .models import SWac

g_log = logging.getLogger(__name__)

# The documentation contradicts itself: the overview names plain HTTP on 80,
# the discovery section advertises 443. Measured against InvisiLED Wall
# hardware (protocol 1.40), the overview wins — port 80 serves the interface
# and 443 refuses the connection outright, despite mDNS advertising it. Hence
# the plain-HTTP default. `ProbeHost` is how to re-check on other hardware.

PORT_HTTP = 80
PORT_HTTPS = 443


def SslctxNoVerify() -> ssl.SSLContext:
	"""A TLS context that accepts the device's self-signed certificate.

	These devices present a certificate no public root will vouch for. This
	protects against passive eavesdropping only — it does not authenticate
	the peer. Fine on a trusted LAN, which is the whole deployment model.
	"""

	sslctx = ssl.create_default_context()
	sslctx.check_hostname = False
	sslctx.verify_mode = ssl.CERT_NONE

	return sslctx


class CTransport:  # tag = trans
	"""An aiohttp session pointed at one device.

	Owns the base URL, TLS policy, timeout, and retry. Use as an async
	context manager so the session is always closed.

	A caller may hand in its own session instead of letting this build one.
	Home Assistant requires that — every integration shares one session from
	`async_get_clientsession` — and it is the right shape for the bridge too,
	which holds several transports at once. An injected session is borrowed,
	never closed.
	"""

	g_cRetryDefault = 2       # extra attempts after the first
	g_dTBackoffBase = 0.5     # seconds; doubles per retry

	def __init__(
		self,
		strHost: str,
		*,
		fTls: bool = False,
		nPort: int | None = None,
		dTTimeout: float = 10.0,
		fVerifyTls: bool = False,
		cRetry: int | None = None,
		session: aiohttp.ClientSession | None = None,
	) -> None:
		self.strHost = strHost
		self.fTls = fTls
		self.nPort = nPort if nPort is not None else (PORT_HTTPS if fTls else PORT_HTTP)
		self.dTTimeout = dTTimeout
		self.fVerifyTls = fVerifyTls
		self.cRetry = cRetry if cRetry is not None else self.g_cRetryDefault

		strScheme = "https" if fTls else "http"
		self.strBaseUrl = f"{strScheme}://{strHost}:{self.nPort}"

		# Both of these used to live on the session — the base URL as
		# `base_url`, the TLS policy as a `TCPConnector`. Neither can be set on
		# a session someone else owns, so both moved to the request instead.
		# ssl=False disables verification wholesale; a context is only needed
		# when we actually intend to verify.

		self.objSsl: ssl.SSLContext | bool
		if not fTls:
			self.objSsl = False
		elif fVerifyTls:
			self.objSsl = ssl.create_default_context()
		else:
			self.objSsl = SslctxNoVerify()

		self.timeout = aiohttp.ClientTimeout(total=dTTimeout)

		self.session = session
		self._fCloseSession = False  # true only for a session we built ourselves

	def StrUrl(self, strUri: str) -> str:
		"""Absolute URL for an endpoint path."""

		return f"{self.strBaseUrl}{strUri}"

	async def __aenter__(self) -> CTransport:
		await self.Open()

		return self

	async def __aexit__(self, *args: object) -> None:
		await self.Close()

	async def Open(self) -> None:
		if self.session is not None:
			return

		self.session = aiohttp.ClientSession()
		self._fCloseSession = True

	async def Close(self) -> None:
		"""Close the session, but only if we were the one that opened it."""

		if self.session is None or not self._fCloseSession:
			return

		await self.session.close()

		self.session = None
		self._fCloseSession = False

	async def ObjPost(self, strUri: str, obj: dict[str, Any]) -> dict[str, Any]:
		"""POST a JSON body and return the decoded response object.

		Raises WacDeviceError when the device reports a non-zero result, so
		callers never have to check `result` themselves.
		"""

		if self.session is None:
			await self.Open()

		assert self.session is not None  # narrowed for mypy; Open() guarantees it

		objResponse = await self._ObjPostRetry(strUri, obj)

		err = ErrFromResponse(objResponse)
		if err is not None:
			raise err

		return objResponse

	async def ObjAction(self, strUri: str, nAction: int, **kwargs: Any) -> dict[str, Any]:
		"""POST an action-carrying body.

		Every endpoint but /device dispatches on an `action` number rather
		than an HTTP verb. Keyword arguments with a value of None are
		dropped — several fields mean something different by their absence
		than by being sent as null.
		"""

		obj: dict[str, Any] = {"action": nAction}
		obj.update({strKey: objValue for strKey, objValue in kwargs.items() if objValue is not None})

		return await self.ObjPost(strUri, obj)

	async def _ObjPostRetry(self, strUri: str, obj: dict[str, Any]) -> dict[str, Any]:
		"""Issue the request, retrying only on failures worth retrying.

		A refused connection or a timeout is worth another attempt; an HTTP
		error status means the device answered and disliked the request, so
		repeating it just wastes time.
		"""

		assert self.session is not None

		errLast: WacError | None = None

		for iAttempt in range(self.cRetry + 1):
			if iAttempt:
				dTBackoff = self.g_dTBackoffBase * (2 ** (iAttempt - 1))
				g_log.debug("retrying POST %s in %.1fs (attempt %d)", strUri, dTBackoff, iAttempt + 1)
				await asyncio.sleep(dTBackoff)

			try:
				async with self.session.post(
					self.StrUrl(strUri),
					json=obj,
					ssl=self.objSsl,
					timeout=self.timeout,
				) as response:
					err = ErrFromStatus(response.status)
					if err is not None:
						raise err

					strBody = await response.text()

			except TimeoutError as exc:
				errLast = WacTimeoutError(f"POST {strUri} timed out after {self.dTTimeout}s")
				errLast.__cause__ = exc
				continue

			except aiohttp.ClientConnectionError as exc:
				errLast = WacTransportError(f"POST {strUri} failed to connect: {exc}")
				errLast.__cause__ = exc
				continue

			except aiohttp.ClientError as exc:
				errLast = WacTransportError(f"POST {strUri} failed: {exc}")
				errLast.__cause__ = exc
				continue

			try:
				objResponse = json.loads(strBody)
			except json.JSONDecodeError as exc:
				raise WacResponseError(f"POST {strUri} returned undecodable JSON: {exc}") from exc

			if not isinstance(objResponse, dict):
				raise WacResponseError(
					f"POST {strUri} returned {type(objResponse).__name__}, expected an object"
				)

			return objResponse

		assert errLast is not None  # the loop runs at least once

		raise errLast


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


class SPortProbe(SWac):  # tag = pprobe
	"""What was learned about one port on a device."""

	nPort: int
	fTcpOpen: bool = False              # did TCP connect at all
	fTls: bool = False                  # was this attempted as TLS
	fTlsHandshake: bool | None = None   # did the TLS handshake succeed
	fCertVerified: bool | None = None   # does the chain verify against public roots
	strCertError: str | None = None     # why verification failed, when it did
	strTlsVersion: str | None = None
	strCipher: str | None = None
	strCertSha256: str | None = None    # fingerprint of the presented certificate
	nHttpStatus: int | None = None      # status from a real /device query
	fJsonBody: bool | None = None       # did that query return a JSON object
	strError: str | None = None         # transport failure, when there was one


class SProbe(SWac):  # tag = probe
	"""The result of probing a host on both candidate ports."""

	strHost: str
	lPortProbe: list[SPortProbe] = Field(default_factory=list)

	def PportprobeAnswering(self) -> SPortProbe | None:
		"""The port that answered a real query, if any."""

		for pprobe in self.lPortProbe:
			if pprobe.fJsonBody:
				return pprobe

		return None


async def _PprobePort(strHost: str, nPort: int, *, fTls: bool, dTTimeout: float) -> SPortProbe:
	"""Probe one port: TCP, then TLS if applicable, then a real request."""

	mpProbe: dict[str, Any] = {"nPort": nPort, "fTls": fTls}

	# 1. Plain TCP reachability.

	try:
		fut = asyncio.open_connection(strHost, nPort)
		reader, writer = await asyncio.wait_for(fut, timeout=dTTimeout)
		writer.close()
		await writer.wait_closed()
		mpProbe["fTcpOpen"] = True
	except TimeoutError:
		# TimeoutError carries no message; saying so beats an empty string.

		mpProbe["fTcpOpen"] = False
		mpProbe["strError"] = f"tcp connect timed out after {dTTimeout:g}s (no response)"

		return SPortProbe.model_validate(mpProbe)

	except OSError as exc:
		mpProbe["fTcpOpen"] = False
		mpProbe["strError"] = f"tcp connect failed: {type(exc).__name__}: {exc}"

		return SPortProbe.model_validate(mpProbe)

	# 2. For TLS ports, find out whether the certificate verifies before
	#    falling back to an unverified handshake to inspect it.

	if fTls:
		try:
			fut = asyncio.open_connection(strHost, nPort, ssl=ssl.create_default_context(), server_hostname=strHost)
			reader, writer = await asyncio.wait_for(fut, timeout=dTTimeout)
			writer.close()
			await writer.wait_closed()
			mpProbe["fCertVerified"] = True
			mpProbe["fTlsHandshake"] = True
		except ssl.SSLCertVerificationError as exc:
			mpProbe["fCertVerified"] = False
			mpProbe["strCertError"] = str(exc)
		except (OSError, TimeoutError) as exc:
			mpProbe["fCertVerified"] = False
			mpProbe["strCertError"] = f"{type(exc).__name__}: {exc}"

		try:
			fut = asyncio.open_connection(strHost, nPort, ssl=SslctxNoVerify(), server_hostname=strHost)
			reader, writer = await asyncio.wait_for(fut, timeout=dTTimeout)

			objSsl = writer.get_extra_info("ssl_object")
			if objSsl is not None:
				mpProbe["fTlsHandshake"] = True
				mpProbe["strTlsVersion"] = objSsl.version()

				tplCipher = objSsl.cipher()
				if tplCipher:
					mpProbe["strCipher"] = tplCipher[0]

				# The certificate cannot be decoded without a parser we do not
				# depend on, but a fingerprint is enough to tell whether it is
				# stable across reboots and matches other devices.

				bCert = objSsl.getpeercert(binary_form=True)
				if bCert:
					mpProbe["strCertSha256"] = hashlib.sha256(bCert).hexdigest()

			writer.close()
			await writer.wait_closed()

		except (ssl.SSLError, OSError, TimeoutError) as exc:
			mpProbe["fTlsHandshake"] = False
			mpProbe["strError"] = f"tls handshake failed: {type(exc).__name__}: {exc}"

			return SPortProbe.model_validate(mpProbe)

	# 3. A real request. Read-only: /device with a query carries no action
	#    and changes nothing.

	try:
		async with CTransport(
			strHost,
			fTls=fTls,
			nPort=nPort,
			dTTimeout=dTTimeout,
			cRetry=0,
		) as trans:
			obj = await trans.ObjPost("/device", {"query": True})
			mpProbe["nHttpStatus"] = 200
			mpProbe["fJsonBody"] = isinstance(obj, dict)

	except WacTransportError as exc:
		mpProbe["nHttpStatus"] = exc.nStatus
		mpProbe["fJsonBody"] = False
		mpProbe["strError"] = str(exc)

	except WacError as exc:
		# The device answered with something parseable enough to classify;
		# that still counts as this port serving the interface.

		mpProbe["fJsonBody"] = True
		mpProbe["strError"] = str(exc)

	return SPortProbe.model_validate(mpProbe)


async def ProbeHost(strHost: str, *, dTTimeout: float = 5.0) -> SProbe:
	"""Determine which port and scheme a device actually serves.

	Read-only: it opens both candidate ports and issues a single /device
	query, which carries no action number and mutates nothing.
	"""

	lPortProbe = [
		await _PprobePort(strHost, PORT_HTTP, fTls=False, dTTimeout=dTTimeout),
		await _PprobePort(strHost, PORT_HTTPS, fTls=True, dTTimeout=dTTimeout),
	]

	return SProbe(strHost=strHost, lPortProbe=lPortProbe)
