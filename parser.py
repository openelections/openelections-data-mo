import unicodecsv

headers = ['county', 'precinct', 'office', 'district', 'party', 'candidate', 'votes']

party_abbrevs = ['REP', 'DEM', 'CST', 'LIB', 'WI', 'WI2', 'WI3', 'WI4', 'WI5', 'WI6']

office_lookup = {'U.S. President And Vice President': 'President', 'Governor': 'Governor',
    'Lieutenant Governor': 'Lieutenant Governor', 'Secretary Of State': 'Secretary of State',
    'State Treasurer': 'State Treasurer', 'Attorney General': 'Attorney General', 'U.S. Representative':
    'U.S. House', 'State Representative': 'State House', 'State Senator': 'State Senate',
    'U.S. Senator': 'U.S. Senate', 'State Auditor': 'State Auditor'}

counties = ['ADAIR','ANDREW','ATCHISON','AUDRAIN','BARRY','BARTON','BATES','BENTON','BOLLINGER','BOONE',
    'BUCHANAN','BUTLER','CALDWELL','CALLAWAY','CAMDEN','CAPE GIRARDEAU','CARROLL','CARTER','CASS','CEDAR',
    'CHARITON','CHRISTIAN','CLARK','CLAY','CLINTON','COLE','COOPER','CRAWFORD','DADE','DALLAS','DAVIESS',
    'DEKALB','DENT','DOUGLAS','DUNKLIN','FRANKLIN','GASCONADE','GENTRY','GREENE','GRUNDY','HARRISON','HENRY',
    'HICKORY','HOLT','HOWARD','HOWELL','IRON','JACKSON','JASPER','JEFFERSON','JOHNSON','KNOX','LACLEDE',
    'LAFAYETTE','LAWRENCE','LEWIS','LINCOLN','LINN','LIVINGSTON','MCDONALD','MACON','MADISON','MARIES','MARION',
    'MERCER','MILLER','MISSISSIPPI','MONITEAU','MONROE','MONTGOMERY','MORGAN','NEW MADRID','NEWTON','NODAWAY',
    'OREGON','OSAGE','OZARK','PEMISCOT','PERRY','PETTIS','PHELPS','PIKE','PLATTE','POLK','PULASKI','PUTNAM',
    'RALLS','RANDOLPH','RAY','REYNOLDS','RIPLEY','ST CHARLES','ST CLAIR','STE GENEVIEVE','ST FRANCOIS','ST LOUIS',
    'ST LOUIS CITY','SALINE','SCHUYLER','SCOTLAND','SCOTT','SHANNON','SHELBY','STODDARD','STONE','SULLIVAN',
    'TANEY','TEXAS','VERNON','WARREN','WASHINGTON','WAYNE','WEBSTER','WORTH','WRIGHT']

def skip_check(line):
    p = False
    if 'General Election' in line:
        p = True
    elif line.strip() == 'Primary':
        p = True
    elif 'enrweb' in line:
        p = True
    elif line.strip() == 'Tuesday, November 07, 2000':
        p = True
    elif line.strip() == '\n':
        p = True
    elif "County Reporting" in line:
        p = True
    elif "Official Election Returns" in line:
        p = True
    elif "State of Missouri" in line:
        p = True
    elif line.strip() == '':
        p = True
    elif 'Board on State Canvassers' in line:
        p = True
    elif 'on Monday, December 04, 2000' in line:
        p = True
    elif 'Office Candidate' in line:
        p = True
    elif 'Total Votes' in line:
        p = True
    elif line[0:4] == 'Dist':
        p = True
#    elif 'Judge' in line:
#        p = True
    elif 'REGISTERED VOTERS' in line:
        p = True
    elif 'BALLOTS CAST' in line:
        p = True
    elif 'of the' in line:
        p = True
    elif 'Vote For' in line.strip():
        p = True
    elif line.strip().split("    ")[0:3] == [u'01', u'02', u'03']:
        p = True
    return p

with open('20001107__mo__general.csv', 'wb') as csvfile:
    w = unicodecsv.writer(csvfile, encoding='utf-8')
    w.writerow(headers)

    lines = open("/Users/derekwillis/Downloads/CountyGeneral2000.txt").readlines()
    keys = []
    for line in lines:
        if skip_check(line):
            continue
        if any(county in line for county in counties):
            c = [county in line for county in counties].index(True)
            county = counties[c]
        elif line.strip() in office_lookup.keys():
        #elif 'Precincts Reporting' in line:
            office = line.strip()#.split('Precincts Reporting')[0].strip()
            if 'District' in office:
                try:
                    office, district = office.split(' District ')
                except:
                    continue
            else:
                district = None
            try:
                office = office_lookup[office.strip()]
            except:
                office = office
            keys = []
        else:
            candidate_bits = [x.strip() for x in line.split('   ') if x.strip() != '']
            if len(candidate_bits) == 4:
                if candidate_bits[1] == 'Yes' or candidate_bits == 'No':
                    continue
                if office != '' and candidate_bits[1] in party_abbrevs:
                    candidate, party, votes, pct = [x for x in candidate_bits if x.strip() != '']
                    votes = votes.replace(',','')
                    w.writerow([county, office, district, party, candidate, votes])
