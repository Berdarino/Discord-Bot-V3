"""Birthday date validation and calendar edge cases."""

import datetime as dt

from discord_bot_v3.cogs.members import _birthday_keys, _parse_birthday


def main() -> None:
    print("== date validation ==")
    assert _parse_birthday("2000-02-29") == dt.date(2000, 2, 29)
    for raw in ("2001-02-29", "03-07-2000", "1899-12-31", "2999-01-01"):
        assert _parse_birthday(raw) is None, raw
    print("   valid leap day accepted; malformed, implausible and future dates refused")

    print("== anniversary keys ==")
    assert _birthday_keys(dt.date(2028, 2, 29)) == ("02-29",)
    assert _birthday_keys(dt.date(2027, 2, 28)) == ("02-28", "02-29")
    assert _birthday_keys(dt.date(2027, 3, 1)) == ("03-01",)
    print("   leap-day birthdays are observed on February 28 in non-leap years")


main()
print("\nMEMBER ASSERTIONS PASSED")
