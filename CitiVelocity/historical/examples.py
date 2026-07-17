"""
examples.py -- worked examples for citivelocity_rates.

Set credentials first:
    export CITI_CLIENT_ID=...
    export CITI_CLIENT_SECRET=...

Then run an example by name, e.g.:
    python examples.py smoke
    python examples.py swaption_vol
    python examples.py swap_curve
    python examples.py treasury
    python examples.py discover
"""

import sys
import logging
import pandas as pd

from citivelocity_rates import CitiVelocityClient, RATES_DATASETS, save

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
pd.set_option("display.width", 160)
pd.set_option("display.max_columns", 30)


def smoke(c: CitiVelocityClient):
    """Smoke test using the throttle-exempt TEST tags (no real entitlement needed).
    TEST.n.FX.FORWARD is documented as excluded from per-item limits."""
    df = c.get_series("TEST.1.FX.FORWARD", "2025-05-08", "2025-05-14", frequency="HOURLY")
    print(df.head(20))
    print(f"\n{len(df)} rows pulled.")


def swaption_vol(c: CitiVelocityClient):
    """USD swaption vol: discover 1M-expiry normal-annual ATM tags, then pull closes."""
    tags = c.list_tags("RATES.VOL.USD.ATM.", regex=".*NORMAL.ANNUAL.1M.*")
    print(f"Found {len(tags)} tags; pulling first {min(len(tags), 20)}.")
    wide = c.get_closes(tags[:20], "2024-01-01", "2024-12-31")
    print(wide.tail())
    save(wide, "usd_swaption_vol_1m.csv")


def swap_curve(c: CitiVelocityClient):
    """USD swap (Libor) par rates across tenors -> wide curve panel."""
    tags = c.find_swap_tags("USD", dataset="RATES.SWAP_LIBOR")
    print(f"Found {len(tags)} swap tags.")
    if tags:
        wide = c.get_closes(tags[:30], "2024-01-01", "2024-12-31")
        print(wide.tail())
        save(wide, "usd_swap_curve.csv")


def treasury(c: CitiVelocityClient):
    """US Treasury on-the-run yields."""
    tags = c.find_govvie_tags("USD", dataset="RATES.TSY")
    print(f"Found {len(tags)} TSY tags.")
    if tags:
        wide = c.get_closes(tags[:20], "2024-01-01", "2024-12-31")
        print(wide.tail())


def discover(c: CitiVelocityClient):
    """Walk the tag tree under RATES and print metadata for a few tags."""
    node = c.browse("RATES")
    print("RATES sub-categories:")
    for k, v in (node.get("fields") or {}).items():
        print(f"  {k}: {v}")
    print("\nKnown rates datasets (reference):")
    for name, info in RATES_DATASETS.items():
        print(f"  {name:22s} since {info['start']}  {info['desc']}")


EXAMPLES = {
    "smoke": smoke,
    "swaption_vol": swaption_vol,
    "swap_curve": swap_curve,
    "treasury": treasury,
    "discover": discover,
}


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if name not in EXAMPLES:
        print(f"Unknown example '{name}'. Choose from: {', '.join(EXAMPLES)}")
        raise SystemExit(1)
    client = CitiVelocityClient()
    EXAMPLES[name](client)
