"""
CSV Generator - Turkish Banking Credit Portfolio Mock Data
==========================================================

Generates realistic mock CSV data files matching the expanded data model:
- Retail Credit records (27 columns, `;` delimited)
- Commercial Credit records (32 columns, `;` delimited)
- Payment Plan / Installment records (14 columns, `;` delimited)

Output directory: test_data/{tenant_id}/
"""

import csv
import random
import os
from datetime import datetime, timedelta


def _random_date(start_days_ago=1000, end_days_ago=30):
    """Generate a random date in YYYYMMDD format."""
    delta = timedelta(days=random.randint(end_days_ago, start_days_ago))
    return (datetime.now() - delta).strftime("%Y%m%d")


def _future_date(from_date_str, months_ahead=12):
    """Generate a future date from a given YYYYMMDD string."""
    base = datetime.strptime(from_date_str, "%Y%m%d")
    future = base + timedelta(days=30 * months_ahead)
    return future.strftime("%Y%m%d")


def generate_retail_loans(tenant_id="BANK001", num_loans=100):
    """
    Generate retail credit records (27 columns) using `;` delimiter.
    """
    os.makedirs(f"test_data/{tenant_id}", exist_ok=True)
    loan_file = f"test_data/{tenant_id}/RETAIL_loans.csv"

    headers = [
        "customer_id", "customer_type", "loan_account_number",
        "loan_status_code", "days_past_due", "final_maturity_date",
        "total_installment_count", "outstanding_installment_count",
        "paid_installment_count", "first_payment_date",
        "original_loan_amount", "outstanding_principal_balance",
        "nominal_interest_rate", "total_interest_amount",
        "kkdf_rate", "kkdf_amount", "bsmv_rate", "bsmv_amount",
        "grace_period_months", "installment_frequency",
        "loan_start_date", "loan_closing_date",
        "insurance_included", "customer_district_code",
        "customer_province_code", "internal_rating", "external_rating",
    ]

    loan_ids = []

    with open(loan_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)

        for i in range(1, num_loans + 1):
            loan_id = f"LOAN_{str(i).zfill(6)}"
            loan_ids.append(loan_id)

            start_date = _random_date(1000, 30)
            total_inst = random.choice([6, 10, 12, 18, 24, 36, 48, 60])
            paid_inst = random.randint(0, total_inst)
            outstanding_inst = total_inst - paid_inst
            status = random.choice(["A", "A", "A", "K", "D"])  # Active bias
            original_amount = round(random.uniform(5000, 500000), 2)
            outstanding = round(original_amount * (outstanding_inst / total_inst), 2)
            rate = round(random.uniform(10, 65), 2)
            dpd = 0 if status == "A" else random.choice([0, 30, 60, 90, 120, 180])

            row = [
                f"CUST_{str(random.randint(1, 50000)).zfill(5)}",
                "I",  # Individual
                loan_id,
                status,
                dpd,
                _future_date(start_date, total_inst),
                total_inst,
                outstanding_inst,
                paid_inst,
                _future_date(start_date, 1),  # first payment ~1 month after start
                original_amount,
                outstanding,
                rate,
                round(original_amount * rate / 100 / 12 * total_inst, 2),
                round(random.uniform(10, 20), 2),  # kkdf_rate
                round(random.uniform(50, 500), 2),  # kkdf_amount
                round(random.uniform(5, 20), 2),    # bsmv_rate
                round(random.uniform(30, 300), 2),  # bsmv_amount
                random.choice([0, 0, 0, 1, 2, 3]),  # grace
                random.choice([1, 1, 1, 3]),         # frequency (mostly monthly)
                start_date,
                "" if status == "A" else _future_date(start_date, paid_inst),
                random.choice(["H", "E"]),           # insurance
                f"DISTRICT_{chr(65 + random.randint(0, 5))}",
                f"PROVINCE_{random.randint(1, 81)}",
                str(random.randint(1, 10)),           # internal_rating
                str(random.randint(300, 1500)),        # external_rating
            ]
            writer.writerow(row)

    print(f"[Retail Loans] Generated: {loan_file} ({num_loans} records)")
    return loan_ids, loan_file


def generate_commercial_loans(tenant_id="BANK002", num_loans=50):
    """
    Generate commercial credit records (32 columns) using `;` delimiter.
    """
    os.makedirs(f"test_data/{tenant_id}", exist_ok=True)
    loan_file = f"test_data/{tenant_id}/COMMERCIAL_loans.csv"

    headers = [
        "loan_account_number", "customer_type", "customer_id",
        "loan_product_type", "loan_status_code", "loan_status_flag",
        "days_past_due", "final_maturity_date",
        "total_installment_count", "outstanding_installment_count",
        "paid_installment_count", "first_payment_date",
        "original_loan_amount", "outstanding_principal_balance",
        "nominal_interest_rate", "total_interest_amount",
        "kkdf_rate", "kkdf_amount", "bsmv_rate", "bsmv_amount",
        "grace_period_months", "installment_frequency",
        "loan_start_date", "loan_closing_date",
        "customer_region_code", "sector_code",
        "internal_credit_rating", "default_probability",
        "risk_class", "customer_segment",
        "internal_rating", "external_rating",
    ]

    loan_ids = []

    with open(loan_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)

        for i in range(1, num_loans + 1):
            loan_id = f"LOAN_COM_{str(i).zfill(4)}"
            loan_ids.append(loan_id)

            start_date = _random_date(1000, 30)
            total_inst = random.choice([1, 4, 6, 12])
            paid_inst = random.randint(0, total_inst)
            outstanding_inst = total_inst - paid_inst
            status = random.choice(["A", "A", "K", "D", "R"])
            original_amount = round(random.uniform(10000, 5000000), 2)
            outstanding = round(original_amount * (outstanding_inst / max(total_inst, 1)), 2)
            rate = round(random.uniform(0, 50), 2)
            dpd = 0 if status in ("A", "K") else random.choice([0, 30, 60, 90])

            row = [
                loan_id,
                random.choice(["T", "V"]),  # Tüzel / Venture
                f"CUST_COM_{str(random.randint(1, 10000)).zfill(5)}",
                str(random.randint(1, 8)),  # product type
                status,
                status,  # flag mirrors status in mock
                dpd,
                _future_date(start_date, total_inst),
                total_inst,
                outstanding_inst,
                paid_inst,
                _future_date(start_date, 1),
                original_amount,
                outstanding,
                rate,
                round(original_amount * rate / 100 / 12 * total_inst, 2),
                round(random.uniform(0, 15), 2),
                round(random.uniform(0, 500), 2),
                round(random.uniform(0, 10), 2),
                round(random.uniform(0, 200), 2),
                random.choice([0, 0, 1, 3]),
                random.choice([1, 3, 6, 12]),
                start_date,
                "" if status == "A" else _future_date(start_date, paid_inst or 1),
                f"REGION_{random.randint(1, 7)}",
                str(random.randint(1, 20)),  # sector
                str(random.randint(1, 10)),  # internal credit rating
                round(random.uniform(0.001, 0.1), 4),
                str(random.randint(1, 5)),
                str(random.randint(1, 4)),
                str(random.randint(1, 10)),
                str(random.randint(300, 1500)),
            ]
            writer.writerow(row)

    print(f"[Commercial Loans] Generated: {loan_file} ({num_loans} records)")
    return loan_ids, loan_file


def generate_payments(tenant_id, loan_type, loan_ids, avg_payments_per_loan=5):
    """
    Generate payment plan / installment records (14+ columns) using `;` delimiter.
    """
    os.makedirs(f"test_data/{tenant_id}", exist_ok=True)
    payment_file = f"test_data/{tenant_id}/{loan_type}_payments.csv"

    headers = [
        "loan_account_number", "installment_number",
        "actual_payment_date", "scheduled_payment_date",
        "installment_amount", "principal_component",
        "interest_component", "kkdf_component", "bsmv_component",
        "installment_status",
        "remaining_principal", "remaining_interest",
        "remaining_kkdf", "remaining_bsmv",
    ]

    record_count = 0

    with open(payment_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)

        for loan_id in loan_ids:
            n_installments = random.randint(1, avg_payments_per_loan * 2)
            base_date = datetime.now() - timedelta(days=random.randint(100, 600))

            for inst_num in range(1, n_installments + 1):
                scheduled = (base_date + timedelta(days=30 * inst_num)).strftime("%Y-%m-%d")
                paid = random.random() < 0.7  # 70% paid
                status = "K" if paid else "A"

                total_installment = round(random.uniform(500, 50000), 2)
                principal = round(total_installment * random.uniform(0.5, 0.8), 2)
                interest = round(total_installment * random.uniform(0.1, 0.3), 2)
                kkdf = round(total_installment * random.uniform(0.01, 0.05), 2)
                bsmv = round(total_installment * random.uniform(0.01, 0.04), 2)

                remaining_p = 0 if paid else round(principal * random.uniform(0.3, 1.0), 2)
                remaining_i = 0 if paid else round(interest * random.uniform(0.3, 1.0), 2)
                remaining_k = 0 if paid else round(kkdf * random.uniform(0.3, 1.0), 2)
                remaining_b = 0 if paid else round(bsmv * random.uniform(0.3, 1.0), 2)

                actual_date = ""
                if paid:
                    # Actual date close to scheduled
                    sched_dt = datetime.strptime(scheduled, "%Y-%m-%d")
                    actual_dt = sched_dt + timedelta(days=random.randint(-5, 10))
                    actual_date = actual_dt.strftime("%Y%m%d")

                row = [
                    loan_id,
                    inst_num,
                    actual_date,
                    scheduled,
                    total_installment,
                    principal,
                    interest,
                    kkdf,
                    bsmv,
                    status,
                    remaining_p,
                    remaining_i,
                    remaining_k,
                    remaining_b,
                ]
                writer.writerow(row)
                record_count += 1

    print(f"[{loan_type} Payments] Generated: {payment_file} ({record_count} records)")
    return payment_file


if __name__ == "__main__":
    print("=" * 60)
    print("Turkish Banking Credit Portfolio - Mock Data Generator")
    print("=" * 60)

    # BANK001: Retail
    retail_ids, _ = generate_retail_loans("BANK001", num_loans=100)
    generate_payments("BANK001", "RETAIL", retail_ids, avg_payments_per_loan=5)

    # BANK002: Commercial
    commercial_ids, _ = generate_commercial_loans("BANK002", num_loans=50)
    generate_payments("BANK002", "COMMERCIAL", commercial_ids, avg_payments_per_loan=3)

    print("=" * 60)
    print("All files generated successfully!")
    print("=" * 60)

    # Large dataset test (optional)
    # retail_ids_big, _ = generate_retail_loans("BANK003", num_loans=100_000)
    # generate_payments("BANK003", "RETAIL", retail_ids_big, avg_payments_per_loan=5)
