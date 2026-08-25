"""
Consolidate ALL extracted 2022 PDF data into one comprehensive county-level CSV
Properly formats office names to match existing data structure
"""

import pandas as pd
import csv
import os
import glob
import re

output_data = []

print("Processing all extracted CSV files...")

# Get all extracted CSV files from pages 1-12 (statewide races with full county data)
csv_files = sorted(glob.glob("Data/2022_page_*_table_*.csv"))
print(f"Found {len(csv_files)} CSV files to process")

def clean_value(val):
    """Clean and convert vote counts"""
    if pd.isna(val):
        return None
    val_str = str(val).strip().replace(',', '')
    try:
        return int(val_str)
    except:
        return None

def extract_party_from_header(header_text):
    """Extract party from candidate header"""
    if 'Republican' in header_text or 'REP' in header_text:
        return 'REP'
    elif 'Democratic' in header_text or 'DEM' in header_text:
        return 'DEM'
    elif 'Libertarian' in header_text or 'LIB' in header_text or 'Lib' in header_text:
        return 'LIB'
    elif 'Constitution' in header_text or 'CON' in header_text:
        return 'CON'
    elif 'Green' in header_text or 'GRN' in header_text:
        return 'GRN'
    return ''

def normalize_office_name(office_name):
    """Normalize office names to match existing data format"""
    office_name = office_name.split('\n')[0] if '\n' in office_name else office_name
    
    # Map to standard names
    office_map = {
        'United States Senator': 'U.S. Senate',
        'Auditor': 'State Auditor',
        'State Treasurer': 'State Treasurer',
        'Secretary of State': 'Secretary of State',
        'Attorney General': 'Attorney General',
        'Governor': 'Governor',
        'Lieutenant Governor': 'Lieutenant Governor'
    }
    
    for pattern, standard in office_map.items():
        if pattern in office_name:
            return standard
    
    return office_name

# Process the main statewide races (pages 1-12 have full county listings)
for page_num in range(1, 13):
    csv_file = f"Data/2022_page_{page_num}_table_1.csv"
    
    if not os.path.exists(csv_file):
        continue
    
    print(f"\nProcessing {csv_file}...")
    
    try:
        df = pd.read_csv(csv_file, header=0)
        
        # Get office name from first cell
        raw_office_name = str(df.columns[0])
        office_name = normalize_office_name(raw_office_name)
        print(f"  Office: {office_name}")
        
        # Extract candidate info from column headers
        candidates = []
        for col_idx in range(1, len(df.columns)):
            header = str(df.columns[col_idx])
            lines = header.split('\n')
            if len(lines) >= 2:
                party = extract_party_from_header(lines[0])
                candidate_name = lines[1].strip()
                candidates.append((candidate_name, party, col_idx))
        
        print(f"  Found {len(candidates)} candidates: {[c[0] for c in candidates]}")
        
        # Process each county row
        for _, row in df.iterrows():
            county_name = str(row.iloc[0]).strip()
            
            # Skip invalid counties
            if not county_name or county_name == 'nan' or 'United States' in county_name or county_name == '':
                continue
            
            # Add data for each candidate
            for candidate_name, party, col_idx in candidates:
                votes = clean_value(row.iloc[col_idx])
                if votes is not None and votes > 0:
                    output_data.append({
                        'county': county_name,
                        'office': office_name,
                        'district': '',
                        'party': party,
                        'candidate': candidate_name,
                        'votes': votes
                    })
        
        race_rows = len([d for d in output_data if d['office'] == office_name])
        print(f"  Processed {race_rows} rows for this race")
        
    except Exception as e:
        print(f"  Error processing {csv_file}: {e}")
        import traceback
        traceback.print_exc()

# Write consolidated output
output_file = "Data/20221108__mo__general__county.csv"
print(f"\n{'='*60}")
print(f"Writing {len(output_data)} total rows to {output_file}...")

with open(output_file, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['county', 'office', 'district', 'party', 'candidate', 'votes'])
    writer.writeheader()
    writer.writerows(output_data)

print(f"✓ Successfully created {output_file}")
print(f"\nSample data (first 10 rows):")
for i in range(min(10, len(output_data))):
    d = output_data[i]
    print(f"  {d['county']:15} | {d['office']:20} | {d['candidate']:25} | {d['party']:3} | {d['votes']:6}")
