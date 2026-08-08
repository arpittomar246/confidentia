"""Reference vocabularies for the synthetic generators.

Real code sets, with weights reflecting rough real-world frequency. This matters
more than it looks: uniformly sampled diagnosis codes produce a flat Gold layer
where every condition is equally common, every aggregate is featureless, and
anomaly detection has nothing to detect. Weighted sampling gives the dashboards
something a clinician or analyst would recognise, and gives the partitioning
experiments realistic skew.

The regulatory framing in the source presentation (HIPAA, PCI-DSS) is US, so the
geography and code sets are US.

Codes are genuine ICD-10-CM / CPT / ISO 18245 MCC values. Descriptions are
abbreviated. No real patient, provider or cardholder data appears anywhere.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------- healthcare

# (code, description, body_system, relative_prevalence)
ICD10_CODES: Final[tuple[tuple[str, str, str, float], ...]] = (
    ("I10", "Essential (primary) hypertension", "circulatory", 12.0),
    ("E11.9", "Type 2 diabetes mellitus without complications", "endocrine", 9.5),
    ("E78.5", "Hyperlipidaemia, unspecified", "endocrine", 8.0),
    ("M54.5", "Low back pain", "musculoskeletal", 6.5),
    ("J45.909", "Unspecified asthma, uncomplicated", "respiratory", 5.0),
    ("F32.9", "Major depressive disorder, single episode", "mental", 4.5),
    ("K21.9", "Gastro-oesophageal reflux without oesophagitis", "digestive", 4.0),
    ("N39.0", "Urinary tract infection, site not specified", "genitourinary", 3.6),
    ("J06.9", "Acute upper respiratory infection, unspecified", "respiratory", 3.4),
    ("R51.9", "Headache, unspecified", "symptoms", 3.0),
    ("E66.9", "Obesity, unspecified", "endocrine", 2.9),
    ("F41.9", "Anxiety disorder, unspecified", "mental", 2.8),
    ("I25.10", "Atherosclerotic heart disease of native coronary artery", "circulatory", 2.6),
    ("M17.9", "Osteoarthritis of knee, unspecified", "musculoskeletal", 2.4),
    ("J44.9", "Chronic obstructive pulmonary disease, unspecified", "respiratory", 2.2),
    ("N18.3", "Chronic kidney disease, stage 3", "genitourinary", 1.9),
    ("I48.91", "Unspecified atrial fibrillation", "circulatory", 1.7),
    ("G47.33", "Obstructive sleep apnoea", "nervous", 1.6),
    ("D64.9", "Anaemia, unspecified", "blood", 1.5),
    ("K57.30", "Diverticulosis of large intestine without perforation", "digestive", 1.3),
    ("L03.115", "Cellulitis of right lower limb", "skin", 1.1),
    ("S72.001A", "Fracture of unspecified part of neck of right femur", "injury", 0.9),
    ("I63.9", "Cerebral infarction, unspecified", "circulatory", 0.8),
    ("C50.911", "Malignant neoplasm of right female breast", "neoplasm", 0.7),
    ("O80", "Encounter for full-term uncomplicated delivery", "pregnancy", 0.6),
    ("A41.9", "Sepsis, unspecified organism", "infectious", 0.5),
    ("T78.40XA", "Allergy, unspecified, initial encounter", "injury", 0.5),
    ("Z00.00", "General adult medical examination without abnormal findings", "factors", 8.5),
    ("Z23", "Encounter for immunisation", "factors", 4.2),
    ("R07.9", "Chest pain, unspecified", "symptoms", 3.2),
)

# (code, description, median_billed_usd)
CPT_CODES: Final[tuple[tuple[str, str, float], ...]] = (
    ("99213", "Office visit, established patient, low complexity", 145.0),
    ("99214", "Office visit, established patient, moderate complexity", 210.0),
    ("99203", "Office visit, new patient, low complexity", 190.0),
    ("99285", "Emergency department visit, high complexity", 1250.0),
    ("99232", "Subsequent hospital care, moderate complexity", 285.0),
    ("80053", "Comprehensive metabolic panel", 65.0),
    ("85025", "Complete blood count with differential", 42.0),
    ("80061", "Lipid panel", 58.0),
    ("83036", "Haemoglobin A1C", 48.0),
    ("71046", "Chest X-ray, two views", 175.0),
    ("74177", "CT abdomen and pelvis with contrast", 1480.0),
    ("70553", "MRI brain with and without contrast", 2650.0),
    ("93000", "Electrocardiogram, complete", 95.0),
    ("45378", "Colonoscopy, diagnostic", 1350.0),
    ("29881", "Arthroscopy, knee, with meniscectomy", 4800.0),
    ("27447", "Total knee arthroplasty", 32000.0),
    ("59400", "Obstetric care, vaginal delivery", 8500.0),
    ("90471", "Immunisation administration", 38.0),
    ("36415", "Collection of venous blood by venipuncture", 22.0),
    ("97110", "Therapeutic exercise, 15 minutes", 68.0),
)

SPECIALTIES: Final[tuple[str, ...]] = (
    "Internal Medicine",
    "Family Medicine",
    "Emergency Medicine",
    "Cardiology",
    "Orthopaedic Surgery",
    "Endocrinology",
    "Pulmonology",
    "Nephrology",
    "Psychiatry",
    "Obstetrics and Gynaecology",
    "Radiology",
    "General Surgery",
    "Gastroenterology",
    "Neurology",
    "Oncology",
)

FACILITY_TYPES: Final[tuple[tuple[str, float], ...]] = (
    ("Outpatient Clinic", 0.46),
    ("Community Hospital", 0.22),
    ("Academic Medical Centre", 0.08),
    ("Urgent Care", 0.14),
    ("Diagnostic Laboratory", 0.10),
)

ENCOUNTER_TYPES: Final[tuple[tuple[str, float], ...]] = (
    ("outpatient", 0.58),
    ("telehealth", 0.14),
    ("emergency", 0.13),
    ("inpatient", 0.09),
    ("observation", 0.06),
)

DISPOSITIONS: Final[tuple[tuple[str, float], ...]] = (
    ("discharged_home", 0.78),
    ("transferred", 0.07),
    ("admitted", 0.09),
    ("left_without_being_seen", 0.04),
    ("expired", 0.02),
)

PAYERS: Final[tuple[tuple[str, str], ...]] = (
    ("Meridian Health Plan", "PPO"),
    ("Northstar Mutual", "HMO"),
    ("Cascade Benefit Group", "PPO"),
    ("Union Federal Health", "EPO"),
    ("Statewide Medicaid", "Medicaid"),
    ("Federal Medicare Part B", "Medicare"),
    ("Lakeshore Assurance", "HMO"),
    ("Pinnacle Care Network", "PPO"),
)

DENIAL_REASONS: Final[tuple[str, ...]] = (
    "CO-16 Missing information",
    "CO-97 Bundled service",
    "CO-45 Charge exceeds fee schedule",
    "PR-1 Deductible amount",
    "CO-29 Time limit for filing expired",
    "CO-11 Diagnosis inconsistent with procedure",
)

# ---------------------------------------------------------------- financial

# (mcc, category, median_amount_usd, lognormal_sigma)
MCC_CODES: Final[tuple[tuple[str, str, float, float], ...]] = (
    ("5411", "Grocery Stores", 62.0, 0.75),
    ("5812", "Eating Places and Restaurants", 38.0, 0.85),
    ("5814", "Fast Food Restaurants", 14.0, 0.60),
    ("5541", "Service Stations", 48.0, 0.55),
    ("5912", "Drug Stores and Pharmacies", 34.0, 0.90),
    ("5311", "Department Stores", 88.0, 1.00),
    ("5732", "Electronics Stores", 240.0, 1.15),
    ("4121", "Taxicabs and Rideshare", 22.0, 0.70),
    ("4899", "Cable and Streaming Services", 18.0, 0.45),
    ("5999", "Miscellaneous Retail", 55.0, 1.05),
    ("4511", "Airlines", 420.0, 0.95),
    ("7011", "Lodging and Hotels", 285.0, 0.85),
    ("5813", "Drinking Places", 46.0, 0.80),
    ("7995", "Betting and Casino Gaming", 130.0, 1.30),
    ("6011", "ATM Cash Withdrawal", 120.0, 0.65),
    ("5942", "Book Stores", 32.0, 0.70),
    ("8062", "Hospitals", 640.0, 1.20),
    ("5651", "Family Clothing Stores", 74.0, 0.95),
    ("7230", "Beauty and Barber Shops", 52.0, 0.60),
    ("5722", "Household Appliance Stores", 380.0, 1.00),
)

CHANNELS: Final[tuple[tuple[str, float], ...]] = (
    ("card_present", 0.44),
    ("ecommerce", 0.34),
    ("atm", 0.09),
    ("recurring", 0.08),
    ("transfer", 0.05),
)

ACCOUNT_TYPES: Final[tuple[tuple[str, float], ...]] = (
    ("checking", 0.52),
    ("savings", 0.24),
    ("credit_card", 0.20),
    ("money_market", 0.04),
)

ACCOUNT_STATUSES: Final[tuple[tuple[str, float], ...]] = (
    ("active", 0.90),
    ("dormant", 0.06),
    ("frozen", 0.025),
    ("closed", 0.015),
)

CURRENCIES: Final[tuple[tuple[str, float], ...]] = (
    ("USD", 0.93),
    ("EUR", 0.03),
    ("GBP", 0.02),
    ("CAD", 0.02),
)

CHARGEBACK_REASONS: Final[tuple[str, ...]] = (
    "10.4 Other Fraud - Card Absent",
    "10.1 EMV Liability Shift Counterfeit",
    "13.1 Merchandise Not Received",
    "12.6 Duplicate Processing",
    "13.7 Cancelled Merchandise",
)

# ----------------------------------------------------------------- geography

# (city, state, zip_prefix, latitude, longitude, population_weight)
CITIES: Final[tuple[tuple[str, str, str, float, float, float], ...]] = (
    ("New York", "NY", "100", 40.7128, -74.0060, 8.3),
    ("Los Angeles", "CA", "900", 34.0522, -118.2437, 3.9),
    ("Chicago", "IL", "606", 41.8781, -87.6298, 2.7),
    ("Houston", "TX", "770", 29.7604, -95.3698, 2.3),
    ("Phoenix", "AZ", "850", 33.4484, -112.0740, 1.6),
    ("Philadelphia", "PA", "191", 39.9526, -75.1652, 1.6),
    ("San Antonio", "TX", "782", 29.4241, -98.4936, 1.5),
    ("San Diego", "CA", "921", 32.7157, -117.1611, 1.4),
    ("Dallas", "TX", "752", 32.7767, -96.7970, 1.3),
    ("Austin", "TX", "787", 30.2672, -97.7431, 1.0),
    ("Seattle", "WA", "981", 47.6062, -122.3321, 0.8),
    ("Denver", "CO", "802", 39.7392, -104.9903, 0.7),
    ("Boston", "MA", "021", 42.3601, -71.0589, 0.7),
    ("Nashville", "TN", "372", 36.1627, -86.7816, 0.7),
    ("Portland", "OR", "972", 45.5152, -122.6784, 0.6),
    ("Minneapolis", "MN", "554", 44.9778, -93.2650, 0.4),
    ("Cleveland", "OH", "441", 41.4993, -81.6944, 0.4),
    ("Tampa", "FL", "336", 27.9506, -82.4572, 0.4),
    ("Pittsburgh", "PA", "152", 40.4406, -79.9959, 0.3),
    ("Salt Lake City", "UT", "841", 40.7608, -111.8910, 0.2),
)

FOREIGN_COUNTRIES: Final[tuple[str, ...]] = ("GB", "DE", "FR", "CA", "AU", "SG", "JP", "BR")

# -------------------------------------------------------------------- names

FIRST_NAMES: Final[tuple[str, ...]] = (
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph",
    "Jessica", "Thomas", "Sarah", "Christopher", "Karen", "Daniel", "Nancy",
    "Matthew", "Lisa", "Anthony", "Margaret", "Mark", "Betty", "Donald", "Sandra",
    "Steven", "Ashley", "Andrew", "Dorothy", "Paul", "Kimberly", "Joshua", "Emily",
    "Kenneth", "Donna", "Kevin", "Michelle", "Brian", "Carol", "George", "Amanda",
    "Timothy", "Melissa", "Ronald", "Deborah", "Aisha", "Wei", "Priya", "Carlos",
    "Fatima", "Hiroshi", "Ingrid", "Omar", "Sofia", "Dmitri",
)

LAST_NAMES: Final[tuple[str, ...]] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
    "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
    "Mitchell", "Carter", "Roberts", "Patel", "Kim", "Okafor", "Novak", "Rossi",
)

EMAIL_DOMAINS: Final[tuple[str, ...]] = (
    "example.com", "mail.example.net", "inbox.example.org", "post.example.io",
)


def weighted(pairs: tuple[tuple[str, float], ...]) -> tuple[list[str], list[float]]:
    """Split ``(value, weight)`` pairs into parallel lists with normalised weights."""
    values = [value for value, _ in pairs]
    weights = [weight for _, weight in pairs]
    total = sum(weights)
    return values, [w / total for w in weights]


def icd10_arrays() -> tuple[list[str], list[str], list[float]]:
    codes = [c for c, _, _, _ in ICD10_CODES]
    systems = [s for _, _, s, _ in ICD10_CODES]
    weights = [w for _, _, _, w in ICD10_CODES]
    total = sum(weights)
    return codes, systems, [w / total for w in weights]
