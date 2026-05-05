#!/usr/bin/env python3
"""
Tax Incentives — Remaining 20 States Seed
==========================================
Run: DATABASE_URL=$NEON_DATABASE_URL python3 /tmp/tax_20_states.py

Sources: NCSL, Stream Data Centers glossary, SDI Alliance, H5 Data Centers,
AbitOs Advisors, MultiState Policy Watch, state revenue departments.
"""
import os, sys
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set"); sys.exit(1)

STATES = [
    {
        "state_abbr": "CA", "state_name": "California",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": True, "energy_incentive": True,
        "data_center_specific": False,
        "incentive_details": "No DC-specific sales tax exemption. California Competes Tax Credit available for large investments. Self-generation incentive program (SGIP) for on-site energy storage. Go-Biz office negotiates case-by-case incentives.",
        "qualifying_investment": "Varies (case-by-case)",
        "qualifying_jobs": "Varies",
        "duration_years": 5,
        "max_benefit": "Up to $180M via CA Competes (shared pool)",
        "notes": "Highest electricity costs in continental US offset by renewable energy access; no DC-specific exemption but large projects negotiate directly"
    },
    {
        "state_abbr": "CT", "state_name": "Connecticut",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": True, "investment_tax_credit": False,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": False,
        "incentive_details": "No DC-specific tax incentive. Enterprise Zone program provides property tax abatements and corporate tax credits in designated areas. Job creation tax credits available through DECD.",
        "qualifying_investment": "Varies",
        "qualifying_jobs": "Varies",
        "duration_years": 10,
        "max_benefit": "80% property tax abatement in enterprise zones",
        "notes": "No DC-specific legislation; general business incentives apply"
    },
    {
        "state_abbr": "DE", "state_name": "Delaware",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": False, "energy_incentive": False,
        "data_center_specific": False,
        "incentive_details": "No state sales tax (beneficial for equipment purchases). No DC-specific incentive program. Delaware Strategic Fund provides discretionary grants and loans for qualifying businesses.",
        "qualifying_investment": "N/A",
        "qualifying_jobs": "N/A",
        "duration_years": 0,
        "max_benefit": "No state sales tax (de facto exemption)",
        "notes": "No sales tax statewide is a natural advantage for DC equipment purchases; no specific DC incentive legislation"
    },
    {
        "state_abbr": "FL", "state_name": "Florida",
        "sales_tax_exempt": True, "property_tax_abatement": False,
        "enterprise_zone": True, "investment_tax_credit": False,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales and use tax exemption for data center property (HB 5003, 2025). As of August 2025, limited to data centers with 100 MW+ IT load. Qualified Target Industry Tax Refund for job creation. Enterprise zones available.",
        "qualifying_investment": "$150M+ (100 MW+ IT load as of 2025)",
        "qualifying_jobs": "30+ new jobs",
        "duration_years": 20,
        "max_benefit": "100% sales tax exemption on qualifying equipment",
        "notes": "2025 law limits exemption to 100MW+ facilities; growing Miami and Jacksonville DC markets"
    },
    {
        "state_abbr": "HI", "state_name": "Hawaii",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": True, "investment_tax_credit": False,
        "job_creation_credit": False, "energy_incentive": True,
        "data_center_specific": False,
        "incentive_details": "No DC-specific tax incentive. Enterprise Zone program available in designated areas. Green Energy Market Securitization program supports renewable energy. High energy costs are primary challenge.",
        "qualifying_investment": "N/A",
        "qualifying_jobs": "N/A",
        "duration_years": 0,
        "max_benefit": "N/A",
        "notes": "No DC-specific incentive; highest electricity costs in US; subsea cable connectivity is key differentiator"
    },
    {
        "state_abbr": "ID", "state_name": "Idaho",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": False, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales tax exemption for qualifying data center equipment. Tax Reimbursement Incentive (TRI) provides tax credits up to 30% on income, payroll, and sales taxes for up to 15 years. Property tax exemption on business personal property.",
        "qualifying_investment": "$10M+",
        "qualifying_jobs": "20+ new jobs",
        "duration_years": 15,
        "max_benefit": "30% tax credit via TRI + sales tax exemption",
        "notes": "Low energy costs; growing Boise market; Idaho Commerce actively recruiting DC investment"
    },
    {
        "state_abbr": "LA", "state_name": "Louisiana",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": True, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales and use tax rebate on qualified equipment and construction costs (Act 730). Industrial Tax Exemption Program (ITEP) provides up to 80% property tax abatement for 10 years. Enterprise Zone program. Quality Jobs Program for payroll rebates.",
        "qualifying_investment": "$200M+",
        "qualifying_jobs": "50+ permanent jobs",
        "duration_years": 20,
        "max_benefit": "100% sales tax rebate + 80% property tax abatement (10yr) + possible 10yr extension",
        "notes": "Act 730 is one of the most aggressive DC incentive packages in the US; LED (Louisiana Economic Development) certification required"
    },
    {
        "state_abbr": "MA", "state_name": "Massachusetts",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": True,
        "data_center_specific": False,
        "incentive_details": "No DC-specific sales tax exemption. Economic Development Incentive Program (EDIP) offers investment tax credits and TIF (tax increment financing). Mass Save energy efficiency incentives. Life sciences and tech sector credits may apply.",
        "qualifying_investment": "Varies (EDIP application)",
        "qualifying_jobs": "Varies",
        "duration_years": 10,
        "max_benefit": "Up to 10% investment tax credit via EDIP",
        "notes": "No DC-specific exemption; high energy costs; Boston market driven by financial services and life sciences"
    },
    {
        "state_abbr": "MD", "state_name": "Maryland",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": True, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales and use tax exemption on computer equipment and electricity for qualifying data centers. Property tax credits in enterprise zones. One Maryland Tax Credit program. More Jobs for Marylanders program provides income tax credits.",
        "qualifying_investment": "$2M+",
        "qualifying_jobs": "Varies by program",
        "duration_years": 10,
        "max_benefit": "100% sales tax exemption + enterprise zone property tax credits",
        "notes": "Low investment threshold ($2M) makes incentives accessible to smaller DCs; proximity to NoVA market; Frederick and Prince George's County growing"
    },
    {
        "state_abbr": "ME", "state_name": "Maine",
        "sales_tax_exempt": True, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales tax refund or exemption for qualified data center projects under 36 M.R.S. § 2021. Facility must be at least 20,000 sq ft. Pine Tree Development Zone credits for job creation in designated areas.",
        "qualifying_investment": "$50M+",
        "qualifying_jobs": "Varies",
        "duration_years": 20,
        "max_benefit": "100% sales tax refund on qualifying equipment",
        "notes": "Cold climate reduces cooling costs; subsea cable connectivity from Europe; 20,000 sq ft minimum facility size"
    },
    {
        "state_abbr": "MI", "state_name": "Michigan",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales and use tax exemption for qualified data center equipment. Personal property tax exemption available through local Industrial Facilities Exemptions. Michigan Business Development Program (MBDP) provides performance-based grants.",
        "qualifying_investment": "$25M+",
        "qualifying_jobs": "25+ new jobs",
        "duration_years": 15,
        "max_benefit": "100% sales tax exemption + up to 50% property tax reduction",
        "notes": "Competitive power costs; Detroit and Grand Rapids emerging DC markets; strong fiber connectivity from Chicago"
    },
    {
        "state_abbr": "MT", "state_name": "Montana",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": False, "energy_incentive": False,
        "data_center_specific": False,
        "incentive_details": "No state sales tax (natural advantage for DC equipment). No DC-specific incentive program. Big Sky Economic Development Trust Fund provides discretionary grants.",
        "qualifying_investment": "N/A",
        "qualifying_jobs": "N/A",
        "duration_years": 0,
        "max_benefit": "No state sales tax (de facto exemption)",
        "notes": "No sales tax statewide; low energy costs from hydropower; cold climate reduces cooling; limited fiber connectivity"
    },
    {
        "state_abbr": "NE", "state_name": "Nebraska",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": False, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "ImagiNE Nebraska Act provides tax credits and exemptions including sales tax exemption, personal property tax exemption, and income tax credits. Nebraska Advantage Act (predecessor) still applies to some projects. LB 1131 (2026) proposes eliminating personal property tax exemption for DCs.",
        "qualifying_investment": "$10M+",
        "qualifying_jobs": "10+ new jobs",
        "duration_years": 10,
        "max_benefit": "100% personal property tax exemption + sales tax refund + income tax credits",
        "notes": "Low energy costs; Omaha is emerging DC market; 2026 legislation may reduce incentives — monitor LB 1131"
    },
    {
        "state_abbr": "NH", "state_name": "New Hampshire",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": False, "energy_incentive": False,
        "data_center_specific": False,
        "incentive_details": "No state sales tax or income tax (natural advantage). No DC-specific incentive program. Local property tax is the primary state/local tax; rates vary significantly by municipality.",
        "qualifying_investment": "N/A",
        "qualifying_jobs": "N/A",
        "duration_years": 0,
        "max_benefit": "No state sales or income tax (de facto exemption)",
        "notes": "No sales tax or income tax statewide; cold climate; proximity to Boston market; Crown Castle metro fiber extends into NH"
    },
    {
        "state_abbr": "NY", "state_name": "New York",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": True, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": True,
        "data_center_specific": True,
        "incentive_details": "Sales tax exemption on equipment purchases for qualifying DCs. START-UP NY provides 10-year tax-free zones near universities. Empire State Development offers Excelsior Jobs Program tax credits. NYPA low-cost power allocations. IDA property tax abatements (PILOT agreements).",
        "qualifying_investment": "$50M+",
        "qualifying_jobs": "25+ new jobs (Excelsior)",
        "duration_years": 15,
        "max_benefit": "100% sales tax exemption + IDA PILOT + NYPA low-cost power",
        "notes": "NYC metro is world's densest carrier hotel market; upstate NY promotes DC development with low-cost hydro power; complex incentive landscape requires IDA negotiation"
    },
    {
        "state_abbr": "RI", "state_name": "Rhode Island",
        "sales_tax_exempt": True, "property_tax_abatement": False,
        "enterprise_zone": True, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "Sales and use tax exemption for data center equipment and electricity. Rebuild Rhode Island Tax Credit program. Enterprise Zone program provides tax credits. Qualified Jobs Incentive Tax Credit Act.",
        "qualifying_investment": "$10M+",
        "qualifying_jobs": "10+ new jobs",
        "duration_years": 20,
        "max_benefit": "100% sales tax exemption on equipment and electricity",
        "notes": "Small state but strategic location between Boston and NYC metro markets; Crown Castle fiber connectivity"
    },
    {
        "state_abbr": "SD", "state_name": "South Dakota",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": False, "energy_incentive": False,
        "data_center_specific": False,
        "incentive_details": "No state income tax or corporate income tax (natural advantage). No DC-specific incentive program. Reinvestment Payment Program available for large capital investments. Governor's Office of Economic Development offers discretionary incentives.",
        "qualifying_investment": "N/A (general programs vary)",
        "qualifying_jobs": "N/A",
        "duration_years": 0,
        "max_benefit": "No state income tax + Reinvestment Payment rebate",
        "notes": "No corporate income tax; low energy costs; cold climate; limited fiber infrastructure; Sioux Falls is primary DC market"
    },
    {
        "state_abbr": "VT", "state_name": "Vermont",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": True, "energy_incentive": True,
        "data_center_specific": False,
        "incentive_details": "No DC-specific tax incentive. Vermont Employment Growth Incentive (VEGI) provides payroll-based tax credits. Renewable energy incentives through Efficiency Vermont. Cold climate advantageous for cooling.",
        "qualifying_investment": "Varies",
        "qualifying_jobs": "Varies (VEGI)",
        "duration_years": 5,
        "max_benefit": "Payroll-based VEGI credits",
        "notes": "No DC-specific legislation; renewable energy focus; small market with limited fiber; cold climate advantage"
    },
    {
        "state_abbr": "WV", "state_name": "West Virginia",
        "sales_tax_exempt": True, "property_tax_abatement": True,
        "enterprise_zone": False, "investment_tax_credit": True,
        "job_creation_credit": True, "energy_incentive": False,
        "data_center_specific": True,
        "incentive_details": "2025 legislation: Sales and use tax exemption on DC equipment. Property tax — DC equipment valued at salvage value (~5%) for ad valorem purposes. Economic Opportunity Tax Credit. High-Tech Manufacturing Credit applicable to DCs.",
        "qualifying_investment": "$50M+",
        "qualifying_jobs": "25+ new jobs",
        "duration_years": 20,
        "max_benefit": "100% sales tax exemption + ~95% property tax reduction (salvage valuation)",
        "notes": "2025 legislation is one of the newest and most aggressive DC incentive packages; low energy costs; proximity to NoVA market"
    },
    {
        "state_abbr": "AK", "state_name": "Alaska",
        "sales_tax_exempt": False, "property_tax_abatement": False,
        "enterprise_zone": False, "investment_tax_credit": False,
        "job_creation_credit": False, "energy_incentive": False,
        "data_center_specific": False,
        "incentive_details": "No state sales tax or income tax. No DC-specific incentive program. Alaska Industrial Development and Export Authority (AIDEA) provides financing for qualifying projects. High energy costs are primary challenge.",
        "qualifying_investment": "N/A",
        "qualifying_jobs": "N/A",
        "duration_years": 0,
        "max_benefit": "No state sales or income tax (de facto exemption)",
        "notes": "No sales tax or income tax; extremely high energy costs; limited fiber (subsea cables to lower 48); cold climate advantage; minimal DC market"
    },
]

def main():
    print("=" * 60)
    print("  Tax Incentives — 20 Remaining States")
    print("=" * 60)
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    cur = conn.cursor()

    # Check existing
    cur.execute("SELECT state_abbr FROM tax_incentives_neon")
    existing = {r[0] for r in cur.fetchall()}
    print(f"  Existing states: {len(existing)}")

    inserted = 0
    skipped = 0
    for s in STATES:
        if s["state_abbr"] in existing:
            skipped += 1
            continue
        cur.execute("""
            INSERT INTO tax_incentives_neon (
                state_abbr, state_name, sales_tax_exempt, property_tax_abatement,
                enterprise_zone, investment_tax_credit, job_creation_credit,
                energy_incentive, data_center_specific, incentive_details,
                qualifying_investment, qualifying_jobs, duration_years,
                max_benefit, notes, last_updated
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
        """, (
            s["state_abbr"], s["state_name"], s["sales_tax_exempt"],
            s["property_tax_abatement"], s["enterprise_zone"],
            s["investment_tax_credit"], s["job_creation_credit"],
            s["energy_incentive"], s["data_center_specific"],
            s["incentive_details"], s["qualifying_investment"],
            s["qualifying_jobs"], s["duration_years"],
            s["max_benefit"], s["notes"],
        ))
        inserted += 1

    conn.commit()

    # Final count
    cur.execute("SELECT COUNT(*) FROM tax_incentives_neon")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tax_incentives_neon WHERE data_center_specific = true")
    dc_specific = cur.fetchone()[0]
    cur.close()
    conn.close()

    print(f"  Inserted: {inserted}")
    print(f"  Skipped (existing): {skipped}")
    print(f"  Total states: {total}")
    print(f"  DC-specific incentives: {dc_specific}")
    print(f"{'=' * 60}")

if __name__ == '__main__':
    main()
