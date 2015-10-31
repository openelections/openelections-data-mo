import time
import urllib2, csv
import mechanize
import unicodedata
from bs4 import BeautifulSoup

def scrape_form(election_id, race_id):
    br = mechanize.Browser()
    br.open('http://enrarchives.sos.mo.gov/enrnet/PickaRace.aspx')

    # Fill out the form
    br.select_form(nr=0)
    br.form['ctl00$MainContent$cboElectionNames'] = [election_id]
    br.form['ctl00$MainContent$cboRaces'] = [race_id]

    # Submit the form
    br.submit('ctl00$MainContent$btnCountyChange')

    # Get HTML
    html = br.response().read()

    # Transform the HTML into a BeautifulSoup object
    soup = BeautifulSoup(html, "html.parser")

    # Find the main table using both the "align" and "class" attributes
    main_table = soup.find('table',
        {'id': 'MainContent_dgrdCountyRaceResults'}
    )

    headers = [cell.text.replace('\r\n','') for cell in main_table.find_all('th')]

    # Now get the data from each table row
    results = []
    for row in main_table.find_all('tr'):
        data = [cell.text for cell in row.find_all('td')]
        if data == []:
            pass
        elif data[0] != u'\xa0':
            results.append(zip(headers,data))
    return results

if __name__ == "__main__":
    csvfile = open('mo_elections.csv', 'rU').readlines()
    output = open('20141104__mo__general.csv', 'wb')
    w = csv.writer(output)
    w.writerow(['county', 'office', 'district', 'party', 'candidate', 'votes'])
    reader = csv.DictReader(csvfile, fieldnames = ['slug', 'election_id', 'race_id', 'office', 'district'])
    reader.next()
    for row in reader:
        time.sleep(2)
        results = scrape_form(row['election_id'], row['race_id'])
        for line in results:
            county = line[0][1]
            for result in line[1:]:
                cp, votes = result
                votes = votes.replace(',','')
                candidate, party = cp.split(", ")
                w.writerow([county, row['office'], row['district'], party, candidate, votes])
