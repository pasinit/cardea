"""ADR-037 question 8 on the host: the `coding` profile reaches its registries, nothing else.

    python3 registry_egress.py [--profile coding] [--arbitrary example.com] [--work DIR]

Reads the installed helper configuration (`/etc/cardea-host-workers.json`, validated by the
installed helper code) and the egress proxy's `/etc/squid/squid.conf`. Then, through the
profile's own proxy port and with `curl`, it checks:

- the proxy configuration carries the profile's egress rules exactly as the helper renders
  them (`cardea.hosts.launcher.egress_rules`), registries as CONNECT to port 443 only;
- every listed registry answers over TLS on port 443 (CONNECT 200);
- an arbitrary host, by default `example.com`, is refused (CONNECT 403);
- a registry over plain HTTP, or on any other port, is refused;
- the host's own loopback, 127.0.0.1, is refused through the proxy.

The unit's own fence (it may reach only its proxy address, ADR-028) is covered by the
ADR-028 probes, `apps/api/tests/hosts/test_confined_debian.py`. This script starts no
service and signals no process; each `curl` is a short-lived child it waits for.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

# `python -I` leaves the script's own directory off the path; the shared modules are there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Evidence,
    add_common_arguments,
    claim,
    free_port,
    run_script,
    use_kernel,
)

NAME = "registry-egress"
SQUID = Path("/etc/squid/squid.conf")


def through(proxy: str, url: str) -> dict[str, Any]:
    """One request through the proxy: the CONNECT status (for https) and the HTTP status."""
    done = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--proxy",
            f"http://{proxy}",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_connect} %{http_code}",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    connect, _, code = done.stdout.strip().partition(" ")
    return {
        "url": url,
        "curl_exit": done.returncode,
        "connect": connect,
        "code": code,
        "stderr": done.stderr[-300:],
    }


def refused(result: dict[str, Any]) -> bool:
    """The proxy said no: 403 to the CONNECT or to the request; never a reply from the target."""
    return result["connect"] == "403" or (
        result["connect"] in {"", "000"} and result["code"] == "403"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_arguments(parser, NAME)
    parser.add_argument("--profile", default="coding", help="the profile to check (%(default)s)")
    parser.add_argument(
        "--arbitrary",
        default="example.com",
        help="a host the profile must not reach (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    use_kernel(args.cardea_lib)
    work = claim(args.work, NAME)
    evidence = Evidence(NAME, work, args.out)

    def body() -> None:
        from cardea.hosts import launcher

        config = launcher.worker_config()
        profile = config["profiles"].get(args.profile)
        if profile is None:
            raise SystemExit(f"the helper has no {args.profile!r} profile")
        network = profile["network"]
        registries = list(network.get("registries", []))
        evidence.observe("network", network)
        evidence.check("profile_lists_registries", bool(registries), registries=registries)
        proxy = network["proxy"]

        installed = [
            line.strip()
            for line in SQUID.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        rules = launcher.egress_rules(args.profile, network)
        missing = [rule for rule in rules if rule not in installed]
        evidence.check("proxy_carries_the_profiles_egress_rules", not missing, missing=missing)

        reached = {host: through(proxy, f"https://{host}/") for host in registries}
        evidence.check(
            "registries_reachable_over_tls",
            bool(reached) and all(r["connect"] == "200" for r in reached.values()),
            results=reached,
        )
        arbitrary = through(proxy, f"https://{args.arbitrary}/")
        evidence.check("arbitrary_host_refused", refused(arbitrary), result=arbitrary)
        if registries:
            plain = through(proxy, f"http://{registries[0]}/")
            other_port = through(proxy, f"https://{registries[0]}:8443/")
            evidence.check("registry_over_plain_http_refused", refused(plain), result=plain)
            evidence.check(
                "registry_on_another_port_refused", refused(other_port), result=other_port
            )
        loopback = [
            through(proxy, "https://127.0.0.1/"),
            through(proxy, f"http://127.0.0.1:{free_port()}/"),
        ]
        evidence.check("host_loopback_refused", all(refused(r) for r in loopback), results=loopback)

    return run_script(evidence, body)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
