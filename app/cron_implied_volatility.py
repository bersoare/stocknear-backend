import requests
import orjson
import re
from datetime import datetime
from dotenv import load_dotenv
import os
import sqlite3
import time
from tqdm import tqdm

load_dotenv()

api_key = os.getenv('UNUSUAL_WHALES_API_KEY')
querystring = {"timeframe":"5Y"}
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


def save_json(data, symbol, directory_path):
    os.makedirs(directory_path, exist_ok=True)  # Ensure the directory exists
    with open(f"{directory_path}/{symbol}.json", 'wb') as file:  # Use binary mode for orjson
        file.write(orjson.dumps(data))


def safe_round(value, decimals=2):
    try:
        return round(float(value), decimals)
    except (ValueError, TypeError):
        return value


def add_data(data, historical_data):
    res_list = []
    for item in data:
        date = item['date']
        for item2 in historical_data:
            try:
                if date == item2['date']:
                    item['changesPercentage'] = item2['changesPercentage']
                    item['putCallRatio'] = item2['putCallRatio']
                    item['total_open_interest'] = item2['total_open_interest']
                    item['changesPercentageOI'] = item2.get('changesPercentageOI',None)
            except Exception as e:
                print(e)
        
        if 'changesPercentage' in item:
            res_list.append(item)
    
    return res_list



def prepare_data(data, symbol, directory_path, sort_by = "date"):
    res_list = []
    for item in data:
        try:
            new_item = {
                key: safe_round(value) if isinstance(value, (int, float, str)) else value
                for key, value in item.items()
            }

            res_list.append(new_item)
        except:
            pass

    if res_list:
        data = sorted(res_list, key=lambda x: x[sort_by], reverse=True)
        with open(f"json/options-historical-data/companies/{symbol}.json", "r") as file:
            historical_data = orjson.loads(file.read())
        
        res_list = add_data(data,historical_data)
        save_json(res_list, symbol, directory_path)



def get_iv_data():
    print("Starting to download iv data...")
    directory_path = "json/implied-volatility"
    total_symbols = get_tickers_from_directory(directory_path)
    if len(total_symbols) < 100:
        total_symbols = stocks_symbols+etf_symbols

    counter = 0
    for symbol in tqdm(total_symbols):
        try:
            url = f"https://api.unusualwhales.com/api/stock/{symbol}/volatility/realized"
            
            response = requests.get(url, headers=headers, params=querystring)
            if response.status_code == 200:
                data = response.json()['data']
                prepare_data(data, symbol, directory_path)
            
            counter +=1
            
            # If 50 chunks have been processed, sleep for 60 seconds
            if counter == 260:
                print("Sleeping...")
                time.sleep(60)
                counter = 0
            
        except Exception as e:
            print(f"Error for {symbol}:{e}")


if __name__ == '__main__':
    get_iv_data()
    
    '''
    directory_path = "json/implied-volatility"
    total_symbols = get_tickers_from_directory(directory_path)
    if len(total_symbols) < 100:
        total_symbols = stocks_symbols+etf_symbols

    for symbol in tqdm(total_symbols):
        try:
            with open(f"json/options-historical-data/companies/{symbol}.json", "r") as file:
                historical_data = orjson.loads(file.read())

            with open(f"json/implied-volatility/{symbol}.json", "r") as file:
                data = orjson.loads(file.read())
            
            res_list = add_data(data,historical_data)

            save_json(res_list, symbol, directory_path)
        except:
            pass
    '''