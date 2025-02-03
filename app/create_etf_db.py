import aiohttp
import asyncio
import sqlite3
import json
import ujson
import pandas as pd
import os
from tqdm import tqdm
import re
import pandas as pd
from datetime import datetime
from data_providers.impl.fmp import FinancialModelingPrep
import warnings
from data_providers.fetcher import get_fetcher
from dotenv import load_dotenv
import os

load_dotenv()
api_key = os.getenv('FMP_API_KEY')
fetcher = get_fetcher(json_mode=True)
fmp = FinancialModelingPrep(fetcher, api_key)


# Filter out the specific RuntimeWarning
warnings.filterwarnings("ignore", category=RuntimeWarning, message="invalid value encountered in scalar divide")


start_date = datetime(2015, 1, 1).strftime("%Y-%m-%d")
end_date = datetime.today().strftime("%Y-%m-%d")



if os.path.exists("backup_db/etf.db"):
    os.remove('backup_db/etf.db')


def get_jsonparsed_data(data):
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {}

def get_etf_provider(etf_name):
    provider_mapping = {
        'first-trust': {'FT', 'First Trust'},
        'blackrock': {'IShares', 'iShares', 'ishares', 'Ishares'},
        'vanguard': {'Vanguard'},
        'state-street': {'SPDR'},
        'invesco': {'Invesco'},
        'charles-schwab': {'Schwab'},
        'jpmorgan-chase': {'JPMorgan Chase', 'J.P.', 'JP Morgan'},
        'dimensional': {'Dimensional'},
        'wisdom-tree': {'Wisdom Tree', 'WisdomTree', 'Wisdom'},
        'proshares': {'ProShares', 'Proshares'},
        'vaneck': {'VanEck'},
        'fidelity': {'Fidelity'},
        'global-x': {'Global X'},
        'american-century-investments': {'Avantis', 'American Century'},
        'direxion': {'Direxion'},
        'goldman-sachs': {'Goldman Sachs'},
        'pimco': {'PIMCO'},
        'flexshares': {'FlexShares'},
        'xtrackers': {'Xtrackers'},
        'capital-group': {'Capital Group'},
        'innovator': {'Innovator'},
        'ark': {'ARK', '3D Printing'},
        'franklin-templeton': {'Franklin', 'Western', 'Royce', 'ClearBridge', 'Martin Currie'},
        'janus-henderson': {'Janus'},
        'ssc': {'Alerian', 'ALPS', 'Alps', 'Riverfront', 'Level Four'},
        'sprott': {'Sprott'},
        'nuveen': {'Nuveen'},
        'victory-shares': {'VictoryShares'},
        'abrdn': {'abrdn'},
        'krane-shares': {'KraneShares'},
        'pgim': {'PGIM'},
        'john-hancock': {'John Hancock'},
        'alpha-architect': {'EA Bridgeway', 'Strive U.S.', 'Freedom 100', 'Alpha Architect', 'Strive', 'Burney', 'Euclidean', 'Gadsden', 'Argent', 'Guru', 'Sparkline', 'Relative Sentiment', 'Altrius Global'},
        'bny-mellon': {'BNY'},
        'amplify-investments': {'Amplify'},
        'the-hartford': {'Hartford'},
        'index-iq': {'IQ', 'IndexIQ'},
        'exchange-traded-concepts': {'ROBO', 'ETC', 'EMQQ', 'Cabana', 'Saba', 'Bitwise', 'NETLease', 'Hull', 'Vesper', 'Corbett', 'FMQQ', 'India Internet', 'QRAFT', 'Capital Link', 'Armor US', 'ETFB Green', 'Nifty India', 'Blue Horizon', 'LG Qraft', 'KPOP', 'Optica Rare', 'Akros', 'BTD Capital'},
        'fm-investments': {'US Treasury', 'F/m'},
        'principal': {'Principal'},
        'etf-mg': {'ETFMG', 'Etho Climate', 'AI Powered Equity', 'Bluestar Israel', 'Breakwave Dry', 'Wedbush'},
        'simplify': {'Simplify'},
        'marygold': {'USCF', 'United States'},
        't-rowe-price': {'T.Rowe Price'},
        'bondbloxx': {'BondBloxx'},
        'columbia-threadneedle': {'Columbia'},
        'tidal': {'RPAR', 'Gotham', 'Adasina', 'UPAR', 'Blueprint Chesapeake', 'Nicholas Fixed', 'FolioBeyond', 'God Bless America', 'Zega Buy', 'Leatherback', 'SonicShares', 'Aztian', 'Unlimited HFND', 'Return Stacked', 'Meet Kevin', 'Sound Enhanced', 'Carbon Collective', 'Pinnacle Focused', 'Robinson Alternative', 'Ionic Inflation', 'ATAC', 'CNIC', 'REIT', 'Newday Ocean'},
        'cambria': {'Cambria'},
        'main-management': {'Main'},
        'allianz': {'AllianzIM'},
        'putnam': {'Putnam'},
        'aptus-capital-advisors': {'Aptus'},
        'yieldmax': {'YieldMax'},
        'graniteshares': {'GraniteShares'},
        'us-global-investors': {'U.S. Global'},
        'the-motley-fool': {'Motley Fool'},
        'inspire': {'Inspire'},
        'defiance': {'Defiance'},
        'harbor': {'Harbor'},
        'advisorshares': {'AdvisorShares'},
        'virtus-investment-partners': {'Virtus'},
        'strategy-shares': {'Strategy Shares'},
        'redwood': {'LeaderShares'},
        'morgan-stanley': {'Calvert', 'Morgan Stanley'},

    }

    for provider, keywords in provider_mapping.items():
        if any(keyword in etf_name for keyword in keywords):
            return provider

    return 'other'


class ETFDatabase:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.cursor.execute("PRAGMA journal_mode = wal")
        self.conn.commit()
        self._create_table()

    def close_connection(self):
        self.cursor.close()
        self.conn.close()

    def _create_table(self):
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS etfs (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            exchange TEXT,
            exchangeShortName TEXT,
            type TEXT
        )
        """)

    def get_column_type(self, value):
        column_type = ""

        if isinstance(value, str):
            column_type = "TEXT"
        elif isinstance(value, int):
            column_type = "INTEGER"
        elif isinstance(value, float):
            column_type = "REAL"
        else:
            # Handle other data types or customize based on your specific needs
            column_type = "TEXT"

        return column_type

    def remove_null(self, value):
        if isinstance(value, str) and value == None:
            value = 'n/a'
        elif isinstance(value, int) and value == None:
            value = 0
        elif isinstance(value, float) and value == None:
            value = 0
        else:
            # Handle other data types or customize based on your specific needs
            pass

        return value

    def delete_data_if_condition(self, condition, symbol):
        # Get a list of all tables in the database
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [table[0] for table in self.cursor.fetchall()]

        for table in tables:
            # Check if the table name is not 'etfs' (the main table)
            if table != 'etfs':
                # Construct a DELETE query to delete data from the table based on the condition
                delete_query = f"DELETE FROM {table} WHERE {condition}"

                # Execute the DELETE query with the symbol as a parameter
                self.cursor.execute(delete_query, (symbol,))
                self.conn.commit()

    async def save_fundamental_data(self, session, symbol):
        try:

            methods = [
                {
                    'method': lambda: fmp.get_etf_info(symbol),
                    'key': 'etf_info'
                },
                {
                    'method': lambda: fmp.get_etf_holder(symbol),
                    'key': 'etf_holder'
                },
                {
                    'method': lambda: fmp.get_etf_country_weightings(symbol),
                    'key': 'etf_country_weightings'
                },
                {
                    'method': lambda: fmp.get_quote(symbol),
                    'key': 'quote'
                },
                {
                    'method': lambda: fmp.get_stock_dividend(symbol),
                    'key': 'stock_dividend'
                },
                {
                    'method': lambda: fmp.get_institutional_holders(symbol, '2023-09-30'),
                    'key': 'institutional_holders'
                }
            ]

            fundamental_data = {}

    
            for method in methods:
                parsed_data = await method['method']()

                try:
                    if isinstance(parsed_data, list) and method['key'] == 'etf_info':
                        fundamental_data['profile'] = ujson.dumps(parsed_data)
                        etf_name = parsed_data[0]['name']
                        etf_provider = get_etf_provider(etf_name)

                        data_dict = {
                                    'inceptionDate': parsed_data[0]['inceptionDate'],
                                    'etfProvider': etf_provider,
                                    'expenseRatio': round(parsed_data[0]['expenseRatio'],2),
                                    'totalAssets': parsed_data[0]['aum'],
                                    }
                        fundamental_data.update(data_dict)

                    elif isinstance(parsed_data, list) and method['key'] == 'quote':
                        fundamental_data['quote'] = ujson.dumps(parsed_data)
                        data_dict = {
                                    'price': parsed_data[0]['price'],
                                    'changesPercentage': round(parsed_data[0]['changesPercentage'],2),
                                    'marketCap': parsed_data[0]['marketCap'],
                                    'volume': parsed_data[0]['volume'],
                                    'avgVolume': parsed_data[0]['avgVolume'],
                                    'eps': round(parsed_data[0]['eps'],2),
                                    'pe': round(parsed_data[0]['pe'],2),
                                    'previousClose': parsed_data[0]['previousClose'],
                                    }
                        fundamental_data.update(data_dict)

                    elif isinstance(parsed_data, list) and method['key'] == 'etf_holder':
                        fundamental_data['holding'] = ujson.dumps(parsed_data)
                        data_dict = {'numberOfHoldings': len(json.loads(fundamental_data['holding']))}
                        fundamental_data.update(data_dict)
                    elif isinstance(parsed_data, list) and method['key'] == 'etf_country_weightings':
                        fundamental_data['country_weightings'] = ujson.dumps(parsed_data)
                        
                    elif isinstance(parsed_data, list) and method['key'] == 'stock_dividend':
                        fundamental_data['etf_dividend'] = ujson.dumps(parsed_data)
        
                    elif isinstance(parsed_data, list) and method['key'] == 'institutional_holders':
                        fundamental_data['shareholders'] = ujson.dumps(parsed_data)

                except:
                    pass


            # Check if columns already exist in the table
            self.cursor.execute("PRAGMA table_info(etfs)")
            columns = {column[1]: column[2] for column in self.cursor.fetchall()}

            # Update column definitions with keys from fundamental_data
            column_definitions = {
                key: (self.get_column_type(fundamental_data.get(key, None)), self.remove_null(fundamental_data.get(key, None)))
                for key in fundamental_data
            }

            '''
            if len(json.loads(fundamental_data['holding'])) == 0:
                self.cursor.execute("DELETE FROM etfs WHERE symbol = ?", (symbol,))
                #self.cursor.execute("DELETE FROM symbol WHERE symbol = ?", (symbol,))
                self.conn.commit()
                print(f"Delete {symbol}")
                return
            '''

            for column, (column_type, value) in column_definitions.items():
                if column not in columns and column_type:
                    self.cursor.execute(f"ALTER TABLE etfs ADD COLUMN {column} {column_type}")

                self.cursor.execute(f"UPDATE etfs SET {column} = ? WHERE symbol = ?", (value, symbol))

            self.conn.commit()

        except Exception as e:
            print(f"Failed to fetch fundamental data for symbol {symbol}: {str(e)}")



    async def save_etfs(self, etfs):
        symbols = []
        names = []
        ticker_data = []

        for etf in etfs:
            exchange_short_name = etf.get('exchangeShortName', '')
            ticker_type = etf.get('type', '')
            symbol = etf.get('symbol', '')
            name = etf.get('name', '')
            exchange = etf.get('exchange', '')

            if (name and '.' not in symbol and not re.search(r'\d', symbol)) or symbol == 'QDVE.DE':
                symbols.append(symbol)
                names.append(name)
                ticker_data.append((symbol, name, exchange, exchange_short_name, ticker_type))
        

        self.cursor.execute("BEGIN TRANSACTION")  # Begin a transaction

        for data in ticker_data:
            symbol, name, exchange, exchange_short_name, ticker_type = data
            self.cursor.execute("""
            INSERT OR IGNORE INTO etfs (symbol, name, exchange, exchangeShortName, type)
            VALUES (?, ?, ?, ?, ?)
            """, (symbol, name, exchange, exchange_short_name, ticker_type))
            self.cursor.execute("""
            UPDATE etfs SET name = ?, exchange = ?, exchangeShortName = ?, type = ?
            WHERE symbol = ?
            """, (name, exchange, exchange_short_name, ticker_type, symbol))

        self.cursor.execute("COMMIT")  # Commit the transaction
        self.conn.commit()

    

        # Save OHLC data for each ticker using aiohttp
        async with aiohttp.ClientSession() as session:
            tasks = []
            i = 0
            for etf_data in tqdm(ticker_data):
                symbol, name, exchange, exchange_short_name, ticker_type = etf_data
                symbol = symbol.replace("-", "")
                tasks.append(self.save_ohlc_data(session, symbol))
                tasks.append(self.save_fundamental_data(session, symbol))

                i += 1
                if i % 150 == 0:
                    await asyncio.gather(*tasks)
                    tasks = []
                    print('sleeping mode: ', i)
                    await asyncio.sleep(60)  # Pause for 60 seconds

            #tasks.append(self.save_ohlc_data(session, "%5EGSPC"))
            
            if tasks:
                await asyncio.gather(*tasks)


    def _create_ticker_table(self, symbol):
        #cleaned_symbol = re.sub(r'[^a-zA-Z0-9_]', '_', symbol)
        # Check if table exists
        self.cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{symbol}'")
        table_exists = self.cursor.fetchone() is not None

        if not table_exists:
            query = f"""
            CREATE TABLE '{cleaned_symbol}' (
                date TEXT,
                open FLOAT,
                high FLOAT,
                low FLOAT,
                close FLOAT,
                volume INT,
                change_percent FLOAT,
            );
            """
            self.cursor.execute(query)

    async def save_ohlc_data(self, session, symbol):
        try:
            #self._create_ticker_table(symbol)  # Create table for the symbol

            ohlc_data = await fmp.get_historical_price_full(symbol, start_date, end_date)
            if 'historical' in ohlc_data:
                ohlc_values = [(item['date'], item['open'], item['high'], item['low'], item['close'], item['volume'], item['changePercent']) for item in ohlc_data['historical'][::-1]]

                df = pd.DataFrame(ohlc_values, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'change_percent'])
            
                # Perform bulk insert
                df.to_sql(symbol, self.conn, if_exists='append', index=False)

        except Exception as e:
            print(f"Failed to create table for symbol {symbol}: {str(e)}")




db = ETFDatabase('backup_db/etf.db')
loop = asyncio.get_event_loop()
all_tickers = loop.run_until_complete(fmp.list_etfs())
'''
for item in all_tickers:
    if item['symbol'] == 'GLD':
        print(item)
'''
loop.run_until_complete(db.save_etfs(all_tickers))
db.close_connection()