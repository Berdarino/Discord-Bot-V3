"""Birthday date validation and calendar edge cases."""

import datetime as dt

from discord_bot_v3.cogs.members import _birthday_keys


def main() -> None:
    print("== anniversary keys ==")
    assert _birthday_keys(dt.date(2028, 2, 29)) == ("02-29",)
    assert _birthday_keys(dt.date(2027, 2, 28)) == ("02-28", "02-29")
    assert _birthday_keys(dt.date(2027, 3, 1)) == ("03-01",)
    print("   leap-day birthdays are observed on February 28 in non-leap years")


main()
print("\nMEMBER ASSERTIONS PASSED")
