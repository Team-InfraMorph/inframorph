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


def estimate_gcp_monthly_krw(services):
    """Reviewed MVP estimate, not a bill or a live pricing lookup.

    Assumes a continuously available shared small Cloud SQL instance and HTTPS
    load balancer, plus one always-active Cloud Run instance per service. Cloud
    Run rounds the shared plan's fractional CPU request up to one whole vCPU.
    """
    foundation_monthly = D("25") + HOURS * D("0.015")
    service_monthly = D("0")
    for service in services:
        vcpu = max(D("1"), (D(service["cpu"]) / 1024).to_integral_value(rounding="ROUND_CEILING"))
        memory_gib = D(service["mem"]) / 1024
        service_monthly += HOURS * (
            vcpu * D("0.0864") + memory_gib * D("0.009")
        )
    monthly_usd = foundation_monthly + service_monthly + D("3")
    return int((monthly_usd * KRW_PER_USD).quantize(D("1"), rounding=ROUND_HALF_UP))
