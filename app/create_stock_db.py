import aiohttp
import asyncio
import sqlite3
import json
import ujson
import pandas as pd
import os
from tqdm import tqdm
import pandas as pd
from datetime import datetime
from ta.utils import *
from ta.volatility import *
from ta.momentum import *
from ta.trend import *
from ta.volume import *
import warnings

from dotenv import load_dotenv
import os
from data_providers.fetcher import get_fetcher
from data_providers.impl.fmp import FinancialModelingPrep

load_dotenv()
api_key = os.getenv('FMP_API_KEY')
fmp = FinancialModelingPrep(get_fetcher(json_mode=True), api_key)

# Filter out the specific RuntimeWarning
warnings.filterwarnings("ignore", category=RuntimeWarning, message="invalid value encountered in scalar divide")


start_date = datetime(2015, 1, 1).strftime("%Y-%m-%d")
end_date = datetime.today().strftime("%Y-%m-%d")

quarter_date = '2024-06-30'


if os.path.exists("backup_db/stocks.db"):
    os.remove('backup_db/stocks.db')


def get_jsonparsed_data(data):
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {}


class StockDatabase:
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
        CREATE TABLE IF NOT EXISTS stocks (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            exchange TEXT,
            exchangeShortName TEXT,
            type TEXT
        )
        """)
        self.conn.commit()


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


    async def save_fundamental_data(self, session, symbol):

        try:

            methods = [
                {
                    'method': lambda: fmp.get_company_profile(symbol),
                    'key': 'profile'
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
                    'method': lambda: fmp.get_employee_count(symbol),
                    'key': 'employee_count'
                },
                {
                    'method': lambda: fmp.get_stock_split(symbol),
                    'key': 'stock_split'
                },
                {
                    'method': lambda: fmp.get_stock_peers(symbol),
                    'key': 'stock_peers'
                },
                {
                    'method': lambda: fmp.get_institutional_holders(symbol, quarter_date),
                    'key': 'institutional_holders'
                },
                {
                    'method': lambda: fmp.get_revenue_product_segmentation(symbol),
                    'key': 'revenue_product_segmentation'
                },
                {
                    'method': lambda: fmp.get_revenue_geographic_segmentation(symbol),
                    'key': 'revenue_geographic_segmentation'
                },
                {
                    'method': lambda: fmp.get_analyst_estimates(symbol),
                    'key': 'analyst_estimates'
                },
            ]

            fundamental_data = {}


            for method in methods:
                parsed_data = await method['method']()

                try:
                    if isinstance(parsed_data, list) and method['key'] == 'profile':
                        # Handle list response, save as JSON object
                        fundamental_data['profile'] = ujson.dumps(parsed_data)
                        data_dict = {
                                    'beta': parsed_data[0]['beta'],
                                    'country': parsed_data[0]['country'],
                                    'sector': parsed_data[0]['sector'],
                                    'industry': parsed_data[0]['industry'],
                                    'discounted_cash_flow': round(parsed_data[0]['dcf'],2),
                                    }
                        fundamental_data.update(data_dict)

                    elif isinstance(parsed_data, list) and method['key'] == 'quote':
                        # Handle list response, save as JSON object
                        fundamental_data['quote'] = ujson.dumps(parsed_data)
                        data_dict = {
                                    'price': parsed_data[0]['price'],
                                    'changesPercentage': round(parsed_data[0]['changesPercentage'],2),
                                    'marketCap': parsed_data[0]['marketCap'],
                                    'volume': parsed_data[0]['volume'],
                                    'avgVolume': parsed_data[0]['avgVolume'],
                                    'eps': parsed_data[0]['eps'],
                                    'pe': parsed_data[0]['pe'],
                                    }
                        fundamental_data.update(data_dict)
                    
                    elif method['key'] == 'stock_dividend':
                        # Handle list response, save as JSON object
                        fundamental_data['stock_dividend'] = ujson.dumps(parsed_data)
                    elif method['key'] == 'employee_count':
                        # Handle list response, save as JSON object
                        fundamental_data['history_employee_count'] = ujson.dumps(parsed_data)
                    elif method['key'] == 'stock_split':
                        # Handle list response, save as JSON object
                        fundamental_data['stock_split'] = ujson.dumps(parsed_data['historical'])
                    elif method['key'] == 'stock_peers':
                        # Handle list response, save as JSON object
                        fundamental_data['stock_peers'] = ujson.dumps([item for item in parsed_data[0]['peersList'] if item != ""])
                    elif method['key'] == 'institutional_holders':
                        # Handle list response, save as JSON object
                        fundamental_data['shareholders'] = ujson.dumps(parsed_data)
                    elif method['key'] == 'shares_float':
                        # Handle list response, save as JSON object
                        fundamental_data['historicalShares'] = ujson.dumps(parsed_data)
                    elif method['key'] == 'revenue_product_segmentation':
                        # Handle list response, save as JSON object
                        fundamental_data['revenue_product_segmentation'] = ujson.dumps(parsed_data)
                    elif method['key'] == 'revenue_geographic_segmentation':
                        # Handle list response, save as JSON object
                        fundamental_data['revenue_geographic_segmentation'] = ujson.dumps(parsed_data)
                    elif method['key'] == 'analyst_estimates':
                        # Handle list response, save as JSON object
                        fundamental_data['analyst_estimates'] = ujson.dumps(parsed_data)
                except Exception as e:
                    print(e)
                    pass


            # Check if columns already exist in the table
            self.cursor.execute("PRAGMA table_info(stocks)")
            columns = {column[1]: column[2] for column in self.cursor.fetchall()}

            # Update column definitions with keys from fundamental_data
            column_definitions = {
                key: (self.get_column_type(fundamental_data.get(key, None)), self.remove_null(fundamental_data.get(key, None)))
                for key in fundamental_data
            }


            for column, (column_type, value) in column_definitions.items():
                if column not in columns and column_type:
                    self.cursor.execute(f"ALTER TABLE stocks ADD COLUMN {column} {column_type}")

                self.cursor.execute(f"UPDATE stocks SET {column} = ? WHERE symbol = ?", (value, symbol))

            self.conn.commit()
        except Exception as e:
            print(f"Failed to fetch fundamental data for symbol {symbol}: {str(e)}")


    async def save_stocks(self, stocks):
        symbols = []
        names = []
        ticker_data = []

        for stock in stocks:
            exchange_short_name = stock.get('exchangeShortName', '')
            ticker_type = stock.get('type', '')
            if exchange_short_name in ['XETRA','NYSE', 'NASDAQ','AMEX', 'PNK','EURONEXT'] and ticker_type in ['stock']:
                symbol = stock.get('symbol', '')
                if exchange_short_name == 'PNK' and symbol not in ['FSRNQ','TSSI','DRSHF','NTDOY','OTGLF','TCEHY', 'KRKNF','BYDDY','XIACY','NSRGY','TLPFY','TLPFF']:
                    pass
                elif exchange_short_name == 'EURONEXT' and symbol not in ['ALEUP.PA','ALNEV.PA','ALGAU.PA','ALDRV.PA','ALHYG.PA','ALVMG.PA','TEP.PA']:
                    pass
                else:
                    name = stock.get('name', '')
                    exchange = stock.get('exchange', '')

                    #if name and '-' not in symbol:
                    if name:
                        symbols.append(symbol)
                        names.append(name)

                        ticker_data.append((symbol, name, exchange, exchange_short_name, ticker_type))
        

        self.cursor.execute("BEGIN TRANSACTION")  # Begin a transaction

        for data in ticker_data:
            symbol, name, exchange, exchange_short_name, ticker_type = data

            # Check if the symbol already exists
            self.cursor.execute("SELECT symbol FROM stocks WHERE symbol = ?", (symbol,))
            exists = self.cursor.fetchone()

            # If it doesn't exist, insert it
            if not exists:
                self.cursor.execute("""
                INSERT INTO stocks (symbol, name, exchange, exchangeShortName, type)
                VALUES (?, ?, ?, ?, ?)
                """, (symbol, name, exchange, exchange_short_name, ticker_type))

            # Update the existing row
            else:
                self.cursor.execute("""
                UPDATE stocks SET name = ?, exchange = ?, exchangeShortName = ?, type = ?
                WHERE symbol = ?
                """, (name, exchange, exchange_short_name, ticker_type, symbol))

        self.conn.commit()

        # Save OHLC data for each ticker using aiohttp
        async with aiohttp.ClientSession() as session:
            tasks = []
            i = 0
            for stock_data in tqdm(ticker_data):
                symbol, name, exchange, exchange_short_name, ticker_type = stock_data
                #symbol = symbol.replace("-", "")  # Remove "-" from symbol
                tasks.append(self.save_ohlc_data(session, symbol))
                tasks.append(self.save_fundamental_data(session, symbol))

                i += 1
                if i % 60 == 0:
                    await asyncio.gather(*tasks)
                    tasks = []
                    print('sleeping mode 30 seconds')
                    await asyncio.sleep(30)  # Pause for 60 seconds

            
            if tasks:
                await asyncio.gather(*tasks)


    def _create_ticker_table(self, symbol):
        cleaned_symbol = symbol  # Ensure this is a safe string to use as a table name
        self.cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS '{cleaned_symbol}' (
                date TEXT UNIQUE,
                open FLOAT,
                high FLOAT,
                low FLOAT,
                close FLOAT,
                volume INT,
                change_percent FLOAT
            );
        """)
        self.conn.commit()

    async def save_ohlc_data(self, session, symbol):
        try:
            self._create_ticker_table(symbol)  # Ensure the table exists

            # Fetch OHLC data from the API
            ohlc_data = await fmp.get_historical_price_full(symbol, start_date, end_date)

            if 'historical' in ohlc_data:
                historical_data = ohlc_data['historical'][::-1]

                for entry in historical_data:
                    # Prepare the data for each entry
                    date = entry.get('date')
                    open_price = entry.get('open')
                    high = entry.get('high')
                    low = entry.get('low')
                    close = entry.get('close')
                    volume = entry.get('volume')
                    change_percent = entry.get('changePercent')

                    # Check if this date's data already exists
                    self.cursor.execute(f"SELECT date FROM '{symbol}' WHERE date = ?", (date,))
                    exists = self.cursor.fetchone()

                    # If it doesn't exist, insert the new data
                    if not exists:
                        self.cursor.execute(f"""
                            INSERT INTO '{symbol}' (date, open, high, low, close, volume, change_percent)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (date, open_price, high, low, close, volume, change_percent))
                
                # Commit all changes to the database
                self.conn.commit()

        except Exception as e:
            print(f"Failed to fetch or insert OHLC data for symbol {symbol}: {str(e)}")



db = StockDatabase('backup_db/stocks.db')
loop = asyncio.get_event_loop()
all_tickers = loop.run_until_complete(fmp.list_available_traded())
all_tickers = [item for item in all_tickers if '-' not in item['symbol'] or item['symbol'] in ['BRK-A', 'BRK-B']]


loop.run_until_complete(db.save_stocks(all_tickers))
db.close_connection()