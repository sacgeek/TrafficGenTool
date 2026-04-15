"""
IP alias manager.

On Linux, each simulated user gets a dedicated IP alias so that traffic
appears to come from distinct source addresses.

Commands issued:
    ip addr add <ip>/24 dev <iface>   (on setup)
    ip addr del <ip>/24 dev <iface>   (on teardown)

The interface and prefix length are configurable.  The manager tracks
which IPs it provisioned so it can clean them all up on shutdown.

Root / CAP_NET_ADMIN is required.  If running without privilege, the
manager logs a warning and skips actual provisioning (useful for dev/test
on a local machine where you still want the rest of the agent to work).
"""

from __future__ import annotations
import asyncio
import ipaddress
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Detect if we're running with net admin capability
def _has_net_admin() -> bool:
    return os.geteuid() == 0


class IPAliasManager:
    """
    Manages IP aliases for a worker node.

    Parameters
    ----------
    ip_range_start : first IP to assign (e.g. "192.168.1.101")
    ip_range_end   : last  IP to assign (e.g. "192.168.1.120")
    interface      : NIC name (e.g. "eth0", "ens3")
    prefix_len     : subnet prefix length (default 24)
    dry_run        : if True, log commands but don't execute them
    """

    def __init__(
        self,
        ip_range_start: str,
        ip_range_end:   str,
        interface:      str  = "eth0",
        prefix_len:     int  = 24,
        dry_run:        bool = False,
    ) -> None:
        self.interface   = interface
        self.prefix_len  = prefix_len
        self.dry_run     = dry_run or not _has_net_admin()

        if not _has_net_admin() and not dry_run:
            logger.warning(
                "IPAliasManager: not running as root — IP alias commands will be "
                "simulated (dry_run=True).  Actual packets will use the primary IP."
            )

        # Build the full IP list
        start = ipaddress.ip_address(ip_range_start)
        end   = ipaddress.ip_address(ip_range_end)
        self._all_ips: list[str] = []
        cur = start
        while cur <= end:
            self._all_ips.append(str(cur))
            cur += 1

        self._provisioned: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available_ips(self) -> list[str]:
        return list(self._all_ips)

    def get_user_ip(self, user_index: int) -> str:
        """Return the IP for the given user index (0-based)."""
        return self._all_ips[user_index % len(self._all_ips)]

    async def provision_all(self) -> list[str]:
        """Add all IP aliases. Returns list of successfully provisioned IPs."""
        results = []
        for ip in self._all_ips:
            ok = await self.provision(ip)
            if ok:
                results.append(ip)
        return results

    async def provision(self, ip: str) -> bool:
        """Add a single IP alias. Returns True on success."""
        if ip in self._provisioned:
            return True
        ok = await self._run_ip_cmd("add", ip)
        if ok:
            self._provisioned.add(ip)
        return ok

    async def teardown(self, ip: str) -> bool:
        """Remove a single IP alias."""
        if ip not in self._provisioned:
            return True
        ok = await self._run_ip_cmd("del", ip)
        if ok:
            self._provisioned.discard(ip)
        return ok

    async def teardown_all(self) -> None:
        """Remove all provisioned IP aliases."""
        for ip in list(self._provisioned):
            await self.teardown(ip)
        logger.info("IPAliasManager: all aliases removed")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_ip_cmd(self, action: str, ip: str) -> bool:
        """
        Run: ip addr {add|del} <ip>/<prefix> dev <iface>
        Returns True on success.
        """
        cmd = [
            "ip", "addr", action,
            f"{ip}/{self.prefix_len}",
            "dev", self.interface,
        ]
        if self.dry_run:
            logger.debug("DRY RUN: %s", " ".join(cmd))
            return True

        logger.debug("Running: %s", " ".join(cmd))
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ),
            )
            if result.returncode != 0:
                # "RTNETLINK answers: File exists" is OK for add (already there)
                if action == "add" and "exists" in result.stderr:
                    self._provisioned.add(ip)
                    return True
                # "Cannot assign requested address" is OK for del (already gone)
                if action == "del" and ("Cannot assign" in result.stderr
                                        or "not found" in result.stderr.lower()):
                    return True
                logger.error("ip addr %s %s failed: %s", action, ip, result.stderr.strip())
                return False
            return True
        except FileNotFoundError:
            logger.error("'ip' command not found — install iproute2")
            return False
        except subprocess.TimeoutExpired:
            logger.error("ip addr %s %s timed out", action, ip)
            return False
        except Exception as exc:
            logger.error("ip addr %s %s error: %s", action, ip, exc)
            return False
