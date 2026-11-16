import requests

HEADERS = {"User-Agent": "Adwait Gaur adwaitgaur197@gmail.com"}
CIK = "0001100663"
TARGET_FORM = "NPORT-P"
TOP_N = 5

url = f"https://data.sec.gov/submissions/CIK{CIK}.json"
r = requests.get(url, headers=HEADERS)
r.raise_for_status()

recent = r.json()["filings"]["recent"]
forms = recent["form"]
dates = recent["filingDate"]
accessions = recent["accessionNumber"]
primary_docs = recent["primaryDocument"]

nport_filings = [
    {"date": dates[i], "accession": accessions[i], "primary_doc": primary_docs[i]}
    for i, form in enumerate(forms)
    if form == TARGET_FORM
]

print(f"Found {len(nport_filings)} {TARGET_FORM} filings for CIK {CIK}.")
print(f"Showing {min(TOP_N, len(nport_filings))} most recent:\n")

for filing in nport_filings[:TOP_N]:
    print(f"  Date      : {filing['date']}")
    print(f"  Accession : {filing['accession']}")
    print(f"  Document  : {filing['primary_doc']}")
    print()
