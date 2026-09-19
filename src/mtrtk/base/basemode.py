"""Base TMODE: survey-in, a fixed site, or off; fixed sites are verified against RTCM 1005."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from mtrtk.base.rtcm1005 import Ecef1005, decode_1005
from mtrtk.config import BaseMode
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.core.state import FixInfo
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import (
    LAYERS_ALL,
    CfgItems,
    tmode_fixed_ecef,
    tmode_off,
    tmode_survey_in,
)
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo

log = logging.getLogger(__name__)

SITE_TOLERANCE_M = 0.0005  # a site is verified when every ECEF axis agrees to half a millimetre
VERIFY_DEADLINE_S = 30.0
DEFAULT_FIXED_ACC_M = 0.01
FIX_TYPE_TIME_ONLY = 5  # NAV-PVT fixType of a receiver sitting on a fixed TMODE position

NO_SITE_REASON = "no active site; falling back to survey-in"
NAK_REASON = "the receiver rejected the TMODE configuration"

# Site identity for "is this still the position we applied?": a renamed, replaced or re-surveyed
# row has to be re-applied, an untouched one must not be.
SiteIdent = tuple[str, int | None, float, float, float]


class ConfigApplier(Protocol):
    async def apply_items(self, items: CfgItems, layers: int = LAYERS_ALL) -> bool: ...


class BaseModeManager:
    """Owns the receiver's TMODE and checks the broadcast ARP against the active site.

    Mode changes are user-initiated, so they are written to every layer (`LAYERS_ALL`) and
    survive a power cycle. Nothing here raises at the receiver: a NAK is announced on
    `base.mode` with a reason, because `run()` must outlive a refused configuration.
    """

    def __init__(
        self,
        bus: Bus,
        controller: ConfigApplier,
        sites: SitesRepo,
        store: StateStore,
        *,
        base_mode: BaseMode,
        svin_min_duration_s: int,
        svin_acc_limit_m: float,
        active_site_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.controller = controller
        self.sites = sites
        self.store = store
        self.mode = base_mode
        self.svin_min_duration_s = svin_min_duration_s
        self.svin_acc_limit_m = svin_acc_limit_m
        self.active_site_name = active_site_name
        self._clock = clock
        self.applied_site: Site | None = None
        self.last_1005: Ecef1005 | None = None
        self.verified = False
        self._mismatch_reported = False
        self._verify_deadline: float | None = None
        self._nak_site: SiteIdent | None = None
        self.sub = bus.subscribe("receiver.capabilities", "rtcm.1005", "state.fix", maxsize=200)

    # ------------------------------------------------------------------- mode
    async def apply_mode(self) -> None:
        """Put the receiver into the configured mode, falling back to survey-in with no site."""
        self._nak_site = None  # a fresh configuration retries a site the receiver refused before
        if self.mode is BaseMode.OFF:
            await self._apply_tmode(tmode_off())
            return
        if self.mode is BaseMode.FIXED:
            site = await self._resolve_site()
            if site is not None:
                await self._apply_fixed(site)
                return
            self.mode = BaseMode.SURVEY_IN
            await self._apply_tmode(self._svin_items(), reason=NO_SITE_REASON)
            return
        await self._apply_tmode(self._svin_items())

    def _svin_items(self) -> CfgItems:
        return tmode_survey_in(self.svin_min_duration_s, self.svin_acc_limit_m)

    async def _resolve_site(self) -> Site | None:
        """The configured site, activated in the database if it is not, else the active one."""
        if self.active_site_name:
            named = await self.sites.get(self.active_site_name)
            if named is not None:
                return named if named.active else await self.sites.activate(named.name)
            log.warning("configured site %r is not in the sites table", self.active_site_name)
        return await self.sites.active()

    async def _apply_tmode(self, items: CfgItems, reason: str | None = None) -> bool:
        """Write a positionless TMODE (off or survey-in) and announce the result."""
        self._clear_verification()
        if not await self.controller.apply_items(items, LAYERS_ALL):
            log.error("receiver NAK'd the %s TMODE configuration", self.mode.value)
            self._announce(None, NAK_REASON)
            return False
        self._announce(None, reason)
        return True

    async def _apply_fixed(self, site: Site) -> bool:
        """Sit the receiver on *site* and open a fresh verification window."""
        acc = site.sigma_3d or DEFAULT_FIXED_ACC_M
        items = tmode_fixed_ecef(site.x, site.y, site.z, acc)
        if not await self.controller.apply_items(items, LAYERS_ALL):
            log.error("receiver NAK'd the fixed position for site %s", site.name)
            self._nak_site = _ident(site)  # do not re-offer it on every poll
            self._announce(site.name, NAK_REASON)
            return False
        self._clear_verification()
        self.mode = BaseMode.FIXED
        self.applied_site = site
        self._verify_deadline = self._clock() + VERIFY_DEADLINE_S
        self._nak_site = None
        log.info(
            "TMODE fixed at site %s (%.4f, %.4f, %.4f) acc %.4f m",
            site.name,
            site.x,
            site.y,
            site.z,
            acc,
        )
        self._announce(site.name)
        return True

    def _clear_verification(self) -> None:
        self.applied_site = None
        self.verified = False
        self._mismatch_reported = False
        self._verify_deadline = None

    def _announce(self, site: str | None, reason: str | None = None) -> None:
        self.bus.publish("base.mode", {"mode": self.mode.value, "site": site, "reason": reason})

    # ------------------------------------------------------------------ sites
    async def freeze_survey_in(self, name: str) -> Site:
        """Store the mean of a completed survey-in as site *name*."""
        svin = self.store.state.survey_in  # a live object: take everything in one read
        x, y, z = svin.mean_x_m, svin.mean_y_m, svin.mean_z_m
        if not svin.valid or x is None or y is None or z is None:
            raise ValueError("the survey-in is not valid yet; wait for it to complete")
        site = Site.from_ecef(
            name,
            x,
            y,
            z,
            sigma_m=svin.mean_acc_m,
            source="survey-in",
            frame="WGS84 (receiver)",
            notes=f"survey-in {svin.dur_s}s, {svin.obs} observations",
        )
        return await self.sites.add(site)

    async def activate_site(self, name: str) -> Site:
        """Make *name* the active site and sit the receiver on it. KeyError if it is unknown."""
        site = await self.sites.activate(name)  # the transaction closes before the receiver write
        await self._apply_fixed(site)
        return site

    async def poll_active_site(self) -> None:
        """Pick up an activation another process made (`mtrtk sites activate`, the API)."""
        active = await self.sites.active()
        if active is None:
            return
        ident = _ident(active)
        if ident == self._nak_site:
            return
        if self.applied_site is not None and _ident(self.applied_site) == ident:
            return
        await self._apply_fixed(active)

    # ----------------------------------------------------------- verification
    def on_1005(self, frame: Frame) -> None:
        """Compare a broadcast ARP with the active site; publishes once per verification edge."""
        ecef = decode_1005(frame)
        if ecef is None:
            return
        self.last_1005 = ecef
        site = self.applied_site
        if self.mode is not BaseMode.FIXED or site is None:
            return
        dx, dy, dz = ecef.x - site.x, ecef.y - site.y, ecef.z - site.z
        meta: dict[str, Any] = {"site": site.name, "dx": dx, "dy": dy, "dz": dz}
        if max(abs(dx), abs(dy), abs(dz)) <= SITE_TOLERANCE_M:
            if not self.verified:
                self.verified = True
                log.info("site %s verified against the broadcast RTCM 1005", site.name)
                self.bus.publish("base.site_verified", meta)
        elif not self._mismatch_reported:
            self._mismatch_reported = True
            self.verified = False
            log.error(
                "broadcast 1005 disagrees with site %s by (%.4f, %.4f, %.4f) m",
                site.name,
                dx,
                dy,
                dz,
            )
            self.bus.publish("base.site_mismatch", meta)

    def on_fix(self, fix: FixInfo) -> None:
        """A fixed base that never reaches fixType 5 is reported once the deadline passes."""
        site = self.applied_site
        if self.mode is not BaseMode.FIXED or site is None:
            return
        if fix.fix_type == FIX_TYPE_TIME_ONLY or self.verified or self._mismatch_reported:
            return
        if self._verify_deadline is None or self._clock() <= self._verify_deadline:
            return
        self._mismatch_reported = True
        reason = (
            f"receiver fixType={fix.fix_type} (expected {FIX_TYPE_TIME_ONLY}) "
            f"{VERIFY_DEADLINE_S:.0f}s after applying the fixed position"
        )
        log.error("site %s did not take effect: %s", site.name, reason)
        self.bus.publish("base.site_mismatch", {"site": site.name, "reason": reason})

    # -------------------------------------------------------------------- run
    def stop(self) -> None:
        """End `run()`: the queued messages still drain before the loop exits."""
        self.bus.unsubscribe(self.sub)

    async def run(self, stop: asyncio.Event, poll_s: float = 10.0) -> None:
        poller = asyncio.create_task(self._poll_loop(stop, poll_s), name="basemode-poll")
        waiter = asyncio.create_task(self._wait_stop(stop), name="basemode-stop")
        try:
            async for topic, item in self.sub:
                # One bad message costs that message, never the mode manager.
                try:
                    await self._handle(topic, item)
                except Exception:
                    log.exception("base mode handling failed for %s", topic)
                if stop.is_set():
                    break
        finally:
            for task in (poller, waiter):
                task.cancel()
            await asyncio.gather(poller, waiter, return_exceptions=True)
            self.stop()

    async def _handle(self, topic: str, item: Any) -> None:
        if topic == "receiver.capabilities":
            await self.apply_mode()
        elif topic == "rtcm.1005":
            self.on_1005(item)
        elif topic == "state.fix":
            self.on_fix(item)

    async def _wait_stop(self, stop: asyncio.Event) -> None:
        """A silent bus must not wedge `run()`: closing the subscription ends the loop."""
        await stop.wait()
        self.stop()

    async def _poll_loop(self, stop: asyncio.Event, poll_s: float) -> None:
        while not stop.is_set():
            await asyncio.sleep(poll_s)
            try:
                await self.poll_active_site()
            except Exception:
                log.exception("active-site poll failed")


def _ident(site: Site) -> SiteIdent:
    return (site.name, site.id, site.x, site.y, site.z)
