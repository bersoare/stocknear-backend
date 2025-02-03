import requests
import orjson
import re
from datetime import datetime
from dotenv import load_dotenv
import os
import sqlite3
import time
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import asyncio
import aiohttp
from data_providers.fetcher import get_fetcher
from data_providers.impl.unusual_whales import UnusualWhales

today = datetime.today().date()


load_dotenv()

api_key = os.getenv('UNUSUAL_WHALES_API_KEY')
headers = {"Accept": "application/json, text/plain", "Authorization": api_key}

# Connect to the databases
con = sqlite3.connect('stocks.db')
etf_con = sqlite3.connect('etf.db')
cursor = con.cursor()
cursor.execute("PRAGMA journal_mode = wal")
#cursor.execute("SELECT DISTINCT symbol FROM stocks WHERE symbol NOT LIKE '%.%' AND marketCap > 1E9")
cursor.execute("SELECT DISTINCT symbol FROM stocks WHERE symbol NOT LIKE '%.%'")
stocks_symbols = [row[0] for row in cursor.fetchall()]

etf_cursor = etf_con.cursor()
etf_cursor.execute("PRAGMA journal_mode = wal")
#etf_cursor.execute("SELECT DISTINCT symbol FROM etfs WHERE marketCap > 1E9")
etf_cursor.execute("SELECT DISTINCT symbol FROM etfs")
etf_symbols = [row[0] for row in etf_cursor.fetchall()]

fetcher = get_fetcher(json_mode=False)
uw = UnusualWhales(fetcher, api_key)

con.close()
etf_con.close()


def get_tickers_from_directory(directory: str):

    try:
        # Ensure the directory exists
        if not os.path.exists(directory):
            raise FileNotFoundError(f"The directory '{directory}' does not exist.")
        
        # Get all tickers from filenames
        return [file.replace(".json", "") for file in os.listdir(directory) if file.endswith(".json")]
    
    except Exception as e:
        print(f"An error occurred: {e}")
        return []

directory_path = "json/hottest-contracts/companies"
total_symbols = get_tickers_from_directory(directory_path)

if len(total_symbols) < 100:
    total_symbols = stocks_symbols+etf_symbols

def save_json(data, symbol,directory="json/hottest-contracts/companies"):
    os.makedirs(directory, exist_ok=True)  # Ensure the directory exists
    with open(f"{directory}/{symbol}.json", 'wb') as file:  # Use binary mode for orjson
        file.write(orjson.dumps(data))


def parse_option_symbol(option_symbol):
    # Define regex pattern to match the symbol structure
    match = re.match(r"([A-Z]+)(\d{6})([CP])(\d+)", option_symbol)
    if not match:
        raise ValueError(f"Invalid option_symbol format: {option_symbol}")
    
    ticker, expiration, option_type, strike_price = match.groups()
    
    # Convert expiration to datetime
    date_expiration = datetime.strptime(expiration, "%y%m%d").date()
    
    # Convert strike price to float
    strike_price = int(strike_price) / 1000

    return date_expiration, option_type, strike_price

def safe_round(value, decimals=2):
    try:
        return round(float(value), decimals)
    except (ValueError, TypeError):
        return value


def prepare_data(data, symbol):

    res_list = []
    for item in data:
        try:
            if float(item['volume']) > 0:
                # Parse option_symbol
                date_expiration, option_type, strike_price = parse_option_symbol(item['option_symbol'])
                if date_expiration >= today:
                    # Round numerical and numerical-string values
                    new_item = {
                        key: safe_round(value) if isinstance(value, (int, float, str)) else value
                        for key, value in item.items()
                    }

                    # Add parsed fields
                    new_item['date_expiration'] = date_expiration
                    new_item['option_type'] = option_type
                    new_item['strike_price'] = strike_price

                    # Calculate open_interest_change
                    new_item['open_interest_change'] = safe_round(
                        new_item.get('open_interest', 0) - new_item.get('prev_oi', 0)
                    )

                    res_list.append(new_item)
        except:
            pass

    if res_list:
        highest_volume = sorted(res_list, key=lambda x: x['volume'], reverse=True)[:10]
        highest_open_interest = sorted(res_list, key=lambda x: x['open_interest'], reverse=True)[:10]
        res_dict = {'volume': highest_volume, 'openInterest': highest_open_interest}
        save_json(res_dict, symbol,"json/hottest-contracts/companies")


def get_hottest_contracts():
    counter = 0
    for symbol in tqdm(total_symbols):
        try:
            response = uw.get_option_contracts(symbol)
            if response.status_code == 200:
                data = response.json()['data']

                prepare_data(data, symbol)
            
            counter +=1
            
            # If 50 chunks have been processed, sleep for 60 seconds
            if counter == 260:
                print("Sleeping...")
                time.sleep(60)
                counter = 0
            
        except Exception as e:
            print(f"Error for {symbol}:{e}")



if __name__ == '__main__':
    get_hottest_contracts()