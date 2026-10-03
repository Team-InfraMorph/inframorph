from decimal import Decimal, ROUND_HALF_UP

D = Decimal
HOURS = D("730")
KRW_PER_USD = D("1600")

def estimate_aws_monthly_krw(services):
    foundation_hourly = (
        2 * D("0.059")
        + D("0.0225")
        + D("0.025")
        + 4 * D("0.005")
    )

    task_hourly = D("0")
    for service in services:
        task_hourly += D(service["cpu"]) / 1024 * D("0.04656")
        task_hourly += D(service["mem"]) / 1024 * D("0.00511")

    monthly_usd = (
        HOURS * (foundation_hourly + task_hourly)
        + 20 * D("0.131")
        + 2 * D("0.40")
        + D("10")
    )
    return int(
        (monthly_usd * KRW_PER_USD).quantize(
            D("1"), rounding=ROUND_HALF_UP
        )
    )