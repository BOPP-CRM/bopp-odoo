def normalize_th_msisdn(phone):
    """Normalize a Thai mobile number to MSISDN digits (66 + 9-digit mobile)."""
    digits = "".join(char for char in str(phone or "").strip() if char.isdigit())
    if not digits:
        return ""

    if digits.startswith("66"):
        mobile = digits[2:]
        if len(mobile) == 9 and mobile[0] in "689":
            return f"66{mobile}"
        if len(mobile) == 10 and mobile.startswith("0"):
            return f"66{mobile[1:]}"
        return digits

    if digits.startswith("0") and len(digits) == 10 and digits[1] in "689":
        return f"66{digits[1:]}"

    if len(digits) == 9 and digits[0] in "689":
        return f"66{digits}"

    return digits


def is_valid_th_msisdn(msisdn):
    return (
        isinstance(msisdn, str)
        and msisdn.startswith("66")
        and len(msisdn) == 11
        and msisdn[2] in "689"
    )
