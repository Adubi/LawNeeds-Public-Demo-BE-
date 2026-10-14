"""
Seeds the subscription_plans table from the SUBSCRIPTION_PLANS constant in
services/subscription_constants.py.

This fixes a pre-existing gap: SUBSCRIPTION_PLANS defines every plan in code,
but nothing in the codebase was writing those definitions into the actual
subscription_plans database table. Since user_subscriptions.plan_id has a
foreign key to subscription_plans.id, an empty table means even basic
freemium signup fails.

Safe to run multiple times: any plan id that already exists in the table is
left untouched (not overwritten, not duplicated).

Usage:
    python seed_subscription_plans.py            # apply
    python seed_subscription_plans.py --check     # dry run, shows what would be inserted
"""
import sys
from dotenv import load_dotenv
load_dotenv()

from services.db import DB
from services.models import SubscriptionPlan
from services.subscription_constants import SUBSCRIPTION_PLANS

# Only these dict keys map to real SubscriptionPlan columns. Any other key
# present in SUBSCRIPTION_PLANS (e.g. "expires_at_midnight" on express-daily)
# is metadata used elsewhere in the codebase, not a DB column, and is
# deliberately ignored here.
COLUMN_KEYS = {
    "id", "name", "tier", "billing_period", "price_gbp", "minutes_included",
    "voice_notes_limit", "uploads_limit", "docgen_limit",
    "max_image_size_mb", "max_doc_pages", "max_docgen_pages",
    "unlimited_ai_queries", "rollover_enabled", "priority_support", "early_access",
    "free_consultation_minutes", "stripe_price_id", "stripe_product_id",
    "is_active", "display_order",
}


def main():
    dry_run = "--check" in sys.argv

    db = DB("LawNeeds")
    with db.get_session() as session:
        existing_ids = {row[0] for row in session.query(SubscriptionPlan.id).all()}

        to_insert = []
        skipped = []
        for plan_id, plan_dict in SUBSCRIPTION_PLANS.items():
            if plan_id in existing_ids:
                skipped.append(plan_id)
                continue
            kwargs = {k: v for k, v in plan_dict.items() if k in COLUMN_KEYS}
            # id comes from the dict's own "id" field already, kept for clarity
            to_insert.append((plan_id, kwargs))

        print(f"Existing plans in DB: {sorted(existing_ids) if existing_ids else '(none)'}")
        print(f"Plans that would be skipped (already exist): {skipped if skipped else '(none)'}")
        print(f"Plans to insert: {[pid for pid, _ in to_insert] if to_insert else '(none)'}")

        if dry_run:
            print("\nDry run -- no changes written. Re-run without --check to apply.")
            return

        if not to_insert:
            print("\nNothing to do -- all plans already present.")
            return

        for plan_id, kwargs in to_insert:
            session.add(SubscriptionPlan(**kwargs))

        session.commit()
        print(f"\nSuccess: inserted {len(to_insert)} plan(s) into subscription_plans.")


if __name__ == "__main__":
    main()