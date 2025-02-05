# Standard library imports
import random
import io
import gzip
import re
import os
import secrets
from benzinga import financial_data
from typing import List, Dict, Set
# Third-party library imports
import numpy as np
import pandas as pd
import orjson
import aiohttp
import aiofiles
import redis
from dotenv import load_dotenv
from pydantic import BaseModel, Field
import requests
from pathlib import Path
import asyncio

# Database related imports
import sqlite3
from contextlib import contextmanager
from pocketbase import PocketBase

# FastAPI and related imports
from fastapi import FastAPI, Depends, HTTPException, Security, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import StreamingResponse, JSONResponse

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from functools import partial
from datetime import datetime
from utils.helper import load_latest_json
import uvicorn

# DB constants & context manager

STOCK_DB = 'stocks'
ETF_DB = 'etf'
CRYPTO_DB = 'crypto'
INSTITUTE_DB = 'institute'

OPTIONS_WATCHLIST_DIR = Path("json/options-historical-data/watchlist")

# Prioritization strategy dictionary
PRIORITY_STRATEGIES = {
    'exact_symbol_match': 0,
    'symbol_prefix_match': 1,
    'exact_name_match': 2,
    'name_prefix_match': 3,
    'symbol_contains': 4,
    'name_contains': 5
}

def calculate_score(item: Dict, search_query: str) -> int:
    name_lower = item['name'].lower()
    symbol_lower = item['symbol'].lower()
    query_lower = search_query.lower()

    if len(query_lower) == 1:
        if symbol_lower == query_lower:
            base_score = PRIORITY_STRATEGIES['exact_symbol_match']
        elif name_lower == query_lower:
            base_score = PRIORITY_STRATEGIES['exact_name_match']
        else:
            base_score = len(PRIORITY_STRATEGIES)
    else:
        if symbol_lower == query_lower:
            base_score = PRIORITY_STRATEGIES['exact_symbol_match']
        elif symbol_lower.startswith(query_lower):
            base_score = PRIORITY_STRATEGIES['symbol_prefix_match']
        elif name_lower == query_lower:
            base_score = PRIORITY_STRATEGIES['exact_name_match']
        elif name_lower.startswith(query_lower):
            base_score = PRIORITY_STRATEGIES['name_prefix_match']
        elif query_lower in symbol_lower:
            base_score = PRIORITY_STRATEGIES['symbol_contains']
        elif query_lower in name_lower:
            base_score = PRIORITY_STRATEGIES['name_contains']
        else:
            base_score = len(PRIORITY_STRATEGIES)

    dot_penalty = 1 if '.' in symbol_lower else 0
    return base_score + dot_penalty


@contextmanager
def db_connection(db_name):
  conn = sqlite3.connect(f'{db_name}.db')
  cursor = conn.cursor()
  cursor.execute("PRAGMA journal_mode = wal")
  try:
    yield cursor
  finally:
    conn.commit()
    cursor.close()
    conn.close()

################# Redis #################
redis_client = redis.Redis(host='redis', port=6379, db=0)
redis_client.flushdb() # TECH DEBT
caching_time = 3600*12 #Cache data for 12 hours

#########################################

#------Start Stocks DB------------#
with db_connection(STOCK_DB) as cursor:
  cursor.execute("SELECT DISTINCT symbol FROM stocks")
  symbols = [row[0] for row in cursor.fetchall()]

  cursor.execute("SELECT symbol, name, type, marketCap FROM stocks")
  raw_data = cursor.fetchall()
  stock_list_data = [{
    'symbol': row[0],
    'name': row[1],
    'type': row[2].capitalize(),
    'marketCap': row[3],
  } for row in raw_data if row[3] is not None]
#------End Stocks DB------------#

#------Start ETF DB------------#
with db_connection(ETF_DB) as cursor:
  cursor.execute("SELECT DISTINCT symbol FROM etfs")
  etf_symbols = [row[0] for row in cursor.fetchall()]

  cursor.execute("SELECT symbol, name, type FROM etfs")
  raw_data = cursor.fetchall()
  etf_list_data = [{
    'symbol': row[0],
    'name': row[1],
    'type': row[2].upper(),
  } for row in raw_data]
#------End ETF DB------------#

#------Start Crypto DB------------#
with db_connection(CRYPTO_DB) as cursor:
  cursor.execute("SELECT DISTINCT symbol FROM cryptos")
  crypto_symbols = [row[0] for row in cursor.fetchall()]

  cursor.execute("SELECT symbol, name, type FROM cryptos")
  raw_data = cursor.fetchall()
  crypto_list_data = [{
    'symbol': row[0],
    'name': row[1],
    'type': row[2].capitalize(),
  } for row in raw_data]
#------End Crypto DB------------#

#------Start Institute DB------------#
with db_connection(INSTITUTE_DB) as cursor:
  cursor.execute("SELECT cik FROM institutes")
  cik_list = [row[0] for row in cursor.fetchall()]
#------End Institute DB------------#

#------Start Stock Screener--------#
with open(f"json/stock-screener/data.json", 'rb') as file:
    stock_screener_data = orjson.loads(file.read())

# Convert stock_screener_data into a dictionary keyed by symbol
stock_screener_data_dict = {item['symbol']: item for item in stock_screener_data}
#------End Stock Screener--------#

#------Init Searchbar Data------------#
searchbar_data = stock_list_data + etf_list_data

for item in searchbar_data:
    try:
        # Look up the symbol in the stock_screener_data_dict
        symbol = item['symbol']
        item['isin'] = stock_screener_data_dict[symbol]['isin']
    except Exception as e:
        item['isin'] = None


etf_set, crypto_set = set(etf_symbols), set(crypto_symbols)


### TECH DEBT ###
con = sqlite3.connect('stocks.db')
etf_con = sqlite3.connect('etf.db')
crypto_con = sqlite3.connect('crypto.db')
con_inst = sqlite3.connect('institute.db')

load_dotenv()

pb = PocketBase('http://127.0.0.1:8090')

FMP_API_KEY = os.getenv('FMP_API_KEY')
Benzinga_API_KEY = os.getenv('BENZINGA_API_KEY')
fin = financial_data.Benzinga(Benzinga_API_KEY)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url = None)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

origins = ["http://www.stocknear.com","https://www.stocknear.com","http://stocknear.com","https://stocknear.com","http://localhost:5173","http://localhost:4173","http://localhost:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



security = HTTPBasic()

#Hide /docs and /openai.json behind authentication
fastapi_username = os.getenv('FASTAPI_USERNAME')
fastapi_password = os.getenv('FASTAPI_PASSWORD')

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, fastapi_username)
    correct_password = secrets.compare_digest(credentials.password, fastapi_password)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


STOCKNEAR_API_KEY = os.getenv('STOCKNEAR_API_KEY')

TEST_USER_API_KEY = 'test'
USER_API_KEY = os.getenv('USER_API_KEY', TEST_USER_API_KEY)

VALID_API_KEYS = [STOCKNEAR_API_KEY, USER_API_KEY]
api_key_header = APIKeyHeader(name="X-API-KEY")

DEFAULT_UVICORN_BASE_URL = '0.0.0.0'
DEFAULT_UVICORN_PORT = 8000


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded"}
    )

async def get_api_key(api_key: str = Security(api_key_header)):
    if api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=403, detail="Could not validate credentials")


@app.get("/docs")
async def get_documentation(username: str = Depends(get_current_username)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="docs")


@app.get("/openapi.json")
async def openapi(username: str = Depends(get_current_username)):
    return get_openapi(title = "FastAPI", version="0.1.0", routes=app.routes)


class TickerData(BaseModel):
    ticker: str

class GeneralData(BaseModel):
    params: str

class ParamsData(BaseModel):
    params: str
    category: str

class MarketNews(BaseModel):
    newsType: str

class OptionsFlowData(BaseModel):
    ticker: str = ''
    start_date: str = ''
    end_date: str = ''
    pagesize: int = Field(default=1000)
    page: int = Field(default=0)

class HistoricalPrice(BaseModel):
    ticker: str
    timePeriod: str

class AnalystId(BaseModel):
    analystId: str

class PoliticianId(BaseModel):
    politicianId: str

class TranscriptData(BaseModel):
    ticker: str
    year: int
    quarter: int

class GetWatchList(BaseModel):
    watchListId: str
    ruleOfList: list

class EditWatchList(BaseModel):
    watchListId: str
    title: str

class GetOnePost(BaseModel):
    postId: str

class UserPost(BaseModel):
    postId: str
    userId: str

class CreateWatchList(BaseModel):
    title: str
    user: str
    ticker: str

class UserId(BaseModel):
    userId: str

class GetLeaderBoard(BaseModel):
    startDate: str
    endDate: str


class GetStrategy(BaseModel):
    strategyId: str

class GetCIKData(BaseModel):
    cik: str

class CreateStrategy(BaseModel):
    title: str
    user: str
    rules: str

class SaveStrategy(BaseModel):
    strategyId: str
    rules: list

class GetFeedback(BaseModel):
    user: str
    rating: str
    description: str

class GetBatchPost(BaseModel):
    userId: str
    startPage: int
    sortingPosts: str
    seenPostId: list

class Liste(BaseModel):
    unreadList: list

class FilterStockList(BaseModel):
    filterList: str

class ETFProviderData(BaseModel):
    etfProvider: str

class IPOData(BaseModel):
    year: str

class HeatMapData(BaseModel):
    index: str

class StockScreenerData(BaseModel):
    ruleOfList: List[str]

class IndicatorListData(BaseModel):
    ruleOfList: list
    tickerList: list

class TransactionId(BaseModel):
    transactionId: str

class InfoText(BaseModel):
    parameter: str

class HistoricalDate(BaseModel):
    date: str

class OptionsWatchList(BaseModel):
    optionsIdList: list


# Replace NaN values with None in the resulting JSON object
def replace_nan_inf_with_none(obj):
    if isinstance(obj, list):
        return [replace_nan_inf_with_none(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: replace_nan_inf_with_none(value) for key, value in obj.items()}
    elif isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    else:
        return obj

def load_json(file_path):
    try:
        with open(file_path, 'rb') as file:
            return orjson.loads(file.read())
    except FileNotFoundError:
        return None

async def load_json_async(file_path):
    # Check if the data is cached in Redis
    cached_data = redis_client.get(file_path)
    if cached_data:
        return orjson.loads(cached_data)

    try:
        with open(file_path, 'r') as f:
            data = orjson.loads(f.read())
            # Cache the data in Redis for 10 minutes
            redis_client.set(file_path, orjson.dumps(data), ex=600)
            return data
    except Exception:
        return None


@app.get("/")
async def hello_world():
    return {"stocknear api"}



@app.post("/correlation-ticker")
async def rating_stock(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"correlation-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/correlation/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*24) # Set cache expiration time to 12 hour

    return res



@app.post("/stock-rating")
async def rating_stock(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"stock-rating-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/ta-rating/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*24)  # Set cache expiration time to 1 day
    return res

@app.post("/historical-price")
async def get_stock(data: HistoricalPrice, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    time_period = data.timePeriod

    cache_key = f"historical-price-{ticker}-{time_period}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/historical-price/{time_period}/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res_json = orjson.dumps(res)
    compressed_data = gzip.compress(res_json)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*24) # Set cache expiration time to Infinity

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )
    
@app.post("/export-price-data")
async def get_stock(data: HistoricalPrice, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    time_period = data.timePeriod
    cache_key = f"export-price-data-{ticker}-{time_period}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    if time_period == 'max':
        try:
            with open(f"json/historical-price/max/{ticker}.json", 'rb') as file:
                res = orjson.loads(file.read())
        except:
            res = []
    else:
        try:
            with open(f"json/export/price/{time_period}/{ticker}.json", 'rb') as file:
                res = orjson.loads(file.read())
        except:
            res = []

    res_json = orjson.dumps(res)
    compressed_data = gzip.compress(res_json)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*24) # Set cache expiration time to Infinity

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/one-day-price")
async def get_stock(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()
    cache_key = f"one-day-price-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/one-day-price/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res_json = orjson.dumps(res)
    compressed_data = gzip.compress(res_json)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*3)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/hover-stock-chart")
async def get_hover_stock_chart(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()
    cache_key = f"hover-stock-chart-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/one-day-price/{ticker}.json", 'rb') as file:
            price_data = orjson.loads(file.read())
        with open(f"json/quote/{ticker}.json", 'rb') as file:
            quote_data = orjson.loads(file.read())
        res = {**quote_data, 'history': price_data}
    except:
        res = {}
    res_json = orjson.dumps(res)
    compressed_data = gzip.compress(res_json)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*3)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



def shuffle_list(lst):
    for i in range(len(lst) - 1, 0, -1):
        j = random.randint(0, i)
        lst[i], lst[j] = lst[j], lst[i]
    return lst



@app.post("/similar-stocks")
async def similar_stocks(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"similar-stocks-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/similar-stocks/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*24)  # Set cache expiration time to 1 day
    return res


@app.post("/similar-etfs")
async def get_similar_etfs(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"similar-etfs-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        query = """
            SELECT symbol, name, totalAssets, numberOfHoldings
            FROM etfs
            WHERE symbol <> ? AND ABS(totalAssets - (
                SELECT totalAssets FROM etfs WHERE symbol = ?
            )) >= 0.2 * (
                SELECT totalAssets FROM etfs WHERE symbol = ?
            )
            ORDER BY totalAssets DESC
            LIMIT 15
        """

        etf_cursor = etf_con.cursor()
        etf_cursor.execute(query, (ticker, ticker, ticker))
        raw_data = etf_cursor.fetchall()

        result = [
            {"symbol": row[0], "name": row[1], "totalAssets": row[2], "numberOfHoldings": row[3]}
            for row in raw_data
        ]

        if len(result) >= 5:
            result = random.sample(result, k=5)

        result.sort(key=lambda x: x["totalAssets"], reverse=True)  # Sort the list in-place

        etf_cursor.close()  # Close the cursor after executing the query
    except:
        result = []

    redis_client.set(cache_key, orjson.dumps(result))
    redis_client.expire(cache_key, 3600*3600)
    return result


@app.post("/market-movers")
async def get_market_movers(data: GeneralData, api_key: str = Security(get_api_key)):
    params = data.params
    cache_key = f"market-movers-{params}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/market-movers/markethours/{params}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 5*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/mini-plots-index")
async def get_market_movers(api_key: str = Security(get_api_key)):
    cache_key = f"get-mini-plots-index"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/mini-plots-index/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 5*60)

    return res



@app.post("/market-news")
async def get_market_news(data: MarketNews, api_key: str = Security(get_api_key)):
    news_type = data.newsType

    cache_key = f"market-news-{news_type}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/market-news/{news_type}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*5)  # Set cache expiration time to 15 min

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/stock-news")
async def stock_news(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"stock-news-{ticker}"

    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})


    try:
        with open(f"json/market-news/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*30)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/stock-press-release")
async def stock_news(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"press-releases-{ticker}"

    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})


    try:
        with open(f"json/market-news/press-releases/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/stock-dividend")
async def stock_dividend(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"stock-dividend-{ticker}"

    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/dividends/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {'history': []}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/stock-quote")
async def stock_dividend(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"stock-quote-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/quote/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60)
    return res

@app.post("/history-employees")
async def history_employees(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"history-employees-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    query_template = """
    SELECT 
        history_employee_count
    FROM 
        stocks 
    WHERE
        symbol = ?
    """

    df = pd.read_sql_query(query_template,con, params=(ticker,))
    try:
        history_employee_count = orjson.loads(df['history_employee_count'].iloc[0])
        res = sorted([entry for entry in history_employee_count if entry["employeeCount"] != 0], key=lambda x: x["filingDate"])
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600) # Set cache expiration time to 1 hour
    return res

@app.post("/stock-income")
async def stock_income(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"stock-income-{ticker}"
    cached_result = redis_client.get(cache_key)

    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/financial-statements/income-statement/quarter/{ticker}.json", 'rb') as file:
            quarter_res = orjson.loads(file.read())
    except:
        quarter_res = []

    try:
        with open(f"json/financial-statements/income-statement/annual/{ticker}.json", 'rb') as file:
            annual_res = orjson.loads(file.read())
    except:
        annual_res = []

    res = {'quarter': quarter_res, 'annual': annual_res}

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/stock-balance-sheet")
async def stock_balance_sheet(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"stock-balance-sheet-{ticker}"
    cached_result = redis_client.get(cache_key)

    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/financial-statements/balance-sheet-statement/quarter/{ticker}.json", 'rb') as file:
            quarter_res = orjson.loads(file.read())
    except:
        quarter_res = []

    try:
        with open(f"json/financial-statements/balance-sheet-statement/annual/{ticker}.json", 'rb') as file:
            annual_res = orjson.loads(file.read())
    except:
        annual_res = []

    res = {'quarter': quarter_res, 'annual': annual_res}

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    ) 

@app.post("/stock-ratios")
async def stock_ratios(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"stock-ratios-{ticker}"
    cached_result = redis_client.get(cache_key)

    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/financial-statements/ratios/quarter/{ticker}.json", 'rb') as file:
            quarter_res = orjson.loads(file.read())
    except:
        quarter_res = []

    try:
        with open(f"json/financial-statements/ratios/annual/{ticker}.json", 'rb') as file:
            annual_res = orjson.loads(file.read())
    except:
        annual_res = []

    res = {'quarter': quarter_res, 'annual': annual_res}

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    ) 


@app.post("/stock-cash-flow")
async def stock_cash_flow(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"stock-cash-flow-{ticker}"
    cached_result = redis_client.get(cache_key)

    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/financial-statements/cash-flow-statement/quarter/{ticker}.json", 'rb') as file:
            quarter_res = orjson.loads(file.read())
    except:
        quarter_res = []

    try:
        with open(f"json/financial-statements/cash-flow-statement/annual/{ticker}.json", 'rb') as file:
            annual_res = orjson.loads(file.read())
    except:
        annual_res = []

    res = {'quarter': quarter_res, 'annual': annual_res}

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    ) 




@app.get("/economic-calendar")
async def economic_calendar(api_key: str = Security(get_api_key)):

    cache_key = f"economic-calendar"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/economic-calendar/calendar.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/earnings-calendar")
async def earnings_calendar(api_key: str = Security(get_api_key)):
    
    cache_key = f"earnings-calendar"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/earnings-calendar/calendar.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []
    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/dividends-calendar")
async def dividends_calendar(api_key: str = Security(get_api_key)):

    cache_key = f"dividends-calendar"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/dividends-calendar/calendar.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/stock-splits-calendar")
async def stock_splits_calendar(api_key: str = Security(get_api_key)):
    cache_key = f"stock-splits-calendar"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/stock-splits-calendar/calendar.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/stockdeck")
async def rating_stock(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"stockdeck-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/stockdeck/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*24)  # Set cache expiration time to 1 day
    return res


@app.post("/analyst-summary-rating")
async def get_analyst_rating(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"analyst-summary-rating-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/analyst/summary/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*60)  # Set cache expiration time to 1 day
    return res

@app.post("/analyst-ticker-history")
async def get_analyst_ticke_history(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"analyst-ticker-history-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/analyst/history/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    # Compress the JSON data
    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*60)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/indicator-data")
async def get_indicator(data: IndicatorListData, api_key: str = Security(get_api_key)):
    rule_of_list = data.ruleOfList or ['volume', 'marketCap', 'changesPercentage', 'price', 'symbol', 'name']
    # Ensure 'symbol' and 'name' are always included in the rule_of_list
    if 'symbol' not in rule_of_list:
        rule_of_list.append('symbol')
    if 'name' not in rule_of_list:
        rule_of_list.append('name')
    
    ticker_list = [t.upper() for t in data.tickerList if t is not None]

    combined_results = []
    
    # Load quote data in parallel
    quote_data = await asyncio.gather(*[load_json_async(f"json/quote/{ticker}.json") for ticker in ticker_list])
    quote_dict = {ticker: data for ticker, data in zip(ticker_list, quote_data) if data}

    # Categorize tickers and extract data
    for ticker, quote in quote_dict.items():
        # Determine the ticker type based on the sets
        ticker_type = (
            'etf' if ticker in etf_set else 
            'crypto' if ticker in crypto_set else 
            'stock'
        )

        # Filter the quote based on keys in rule_of_list (use data only from quote.json for these)
        filtered_quote = {key: quote.get(key) for key in rule_of_list if key in quote}
        filtered_quote['type'] = ticker_type
        # Add the result to combined_results
        combined_results.append(filtered_quote)

    # Fetch and merge data from stock_screener_data, but exclude price, volume, and changesPercentage
    screener_keys = [key for key in rule_of_list if key not in ['volume', 'marketCap', 'changesPercentage', 'price', 'symbol', 'name']]
    if screener_keys:
        screener_dict = {item['symbol']: {k: v for k, v in item.items() if k in screener_keys} for item in stock_screener_data}
        for result in combined_results:
            symbol = result.get('symbol')
            if symbol in screener_dict:
                # Only merge screener data for keys that are not price, volume, or changesPercentage
                result.update(screener_dict[symbol])

            
    # Serialize and compress the response
    res = orjson.dumps(combined_results)
    compressed_data = gzip.compress(res)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



async def process_watchlist_ticker(ticker, rule_of_list, quote_keys_to_include, screener_dict, etf_set, crypto_set):
    """Optimized single ticker processing with reduced I/O and memory overhead."""
    ticker = ticker.upper()
    ticker_type = 'stocks'
    if ticker in etf_set:
        ticker_type = 'etf'
    elif ticker in crypto_set:
        ticker_type = 'crypto'

    try:
        # Combine I/O operations into single async read
        quote_dict, news_dict, earnings_dict = await asyncio.gather(
            load_json_async(f"json/quote/{ticker}.json"),
            load_json_async(f"json/market-news/companies/{ticker}.json"),
            load_json_async(f"json/earnings/next/{ticker}.json")
        )
    except Exception:
        # Gracefully handle missing files
        return None, [], None

    # Early return if no quote data
    if not quote_dict:
        return None, [], None

    # Optimized quote filtering with dict comprehension
    filtered_quote = {
        key: quote_dict.get(key) 
        for key in rule_of_list + quote_keys_to_include 
        if key in quote_dict
    }
    
    # Efficiently add core quote data
    core_keys = ['price', 'volume', 'changesPercentage', 'symbol']
    for key in core_keys:
        if key in quote_dict:
            filtered_quote[key] = quote_dict[key]
    
    filtered_quote['type'] = ticker_type

    # Efficient screener data merge
    symbol = filtered_quote.get('symbol')
    if symbol and symbol in screener_dict:
        # Use dict.update() for faster merging
        screener_data = {
            k: v for k, v in screener_dict[symbol].items() 
            if k not in filtered_quote
        }
        filtered_quote.update(screener_data)

    # Lightweight news processing
    news = [
        {k: v for k, v in item.items() if k not in ['image', 'text']}
        for item in (news_dict or [])[:5]
    ]

    # Compact earnings processing
    earnings = {**earnings_dict, 'symbol': symbol} if earnings_dict else None

    return filtered_quote, news, earnings

@app.post("/get-watchlist")
async def get_watchlist(data: GetWatchList, api_key: str = Security(get_api_key)):
    """Further optimized watchlist endpoint with concurrent processing."""
    try:
        watchlist = pb.collection("watchlist").get_one(data.watchListId)
        ticker_list = watchlist.ticker
    except Exception:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    # Predefined configurations
    quote_keys_to_include = ['volume', 'marketCap', 'changesPercentage', 'price', 'symbol', 'name']
    rule_of_list = list(set(
        (data.ruleOfList or []) + 
        ['symbol', 'name']
    ))

    # Prepare screener dictionary with efficient lookup
    screener_dict = {
        item['symbol']: {
            key: item.get(key) 
            for key in rule_of_list if key in item
        }
        for item in stock_screener_data
    }

    # Use concurrent processing with more efficient method
    results_and_extras = await asyncio.gather(
        *[
            process_watchlist_ticker(
                ticker, 
                rule_of_list, 
                quote_keys_to_include, 
                screener_dict,
                etf_set,  # Assuming these are pre-computed sets
                crypto_set
            ) 
            for ticker in ticker_list
        ]
    )

    # Efficient list comprehensions for filtering
    combined_results = [result for result in (r[0] for r in results_and_extras) if result]
    combined_news = [
        news_item 
        for news_list in (r[1] for r in results_and_extras) 
        for news_item in news_list
    ]
    combined_earnings = [earnings for earnings in (r[2] for r in results_and_extras) if earnings]

    # Use orjson for faster JSON serialization
    res = {
        'data': combined_results, 
        'news': combined_news,
        'earnings': combined_earnings
    }

    # Compress efficiently
    compressed_data = gzip.compress(orjson.dumps(res), compresslevel=6)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/get-price-alert")
async def get_price_alert(data: dict, api_key: str = Security(get_api_key)):
    user_id = data.get('userId')
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")
    
    # Fetch all alerts for the user in a single database call
    try:
        result = pb.collection("priceAlert").get_full_list(
            query_params={"filter": f"user='{user_id}' && triggered=false"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query failed: {str(e)}")
    
    # Extract unique tickers
    unique_tickers = {item.symbol for item in result if hasattr(item, 'symbol')}
    
    async def fetch_ticker_data(ticker):
        try:
            news_task = load_json_async(f"json/market-news/companies/{ticker}.json")
            earnings_task = load_json_async(f"json/earnings/next/{ticker}.json")
            
            news_dict, earnings_dict = await asyncio.gather(news_task, earnings_task)
            
            # Process news
            news = []
            if news_dict:
                news = [
                    {key: value for key, value in item.items() if key not in ['image', 'text']}
                    for item in news_dict[:5]
                ]
            
            # Process earnings
            earnings = None
            if earnings_dict:
                earnings = {**earnings_dict, 'symbol': ticker}
            
            return news, earnings
        except Exception as e:
            print(f"Error fetching data for {ticker}: {e}")
            return [], None
    
    async def fetch_quote_data(item):
        try:
            async with aiofiles.open(f"json/quote/{item.symbol}.json", mode='r') as file:
                quote_data = orjson.loads(await file.read())
            
            return {
                'symbol': item.symbol,
                'name': getattr(item, 'name', ''),
                'id': item.id,
                'type': getattr(item, 'asset_type', '').lower().replace("stock", "stocks"),
                'targetPrice': getattr(item, 'target_price', None),
                'condition': getattr(item, 'condition', '').capitalize(),
                'priceWhenCreated': getattr(item, 'price_when_created', None),
                'price': quote_data.get("price"),
                'changesPercentage': quote_data.get("changesPercentage"),
                'volume': quote_data.get("volume"),
            }
        except FileNotFoundError:
            print(f"Quote file not found for {item.symbol}")
            return None
        except Exception as e:
            print(f"Error processing {item.symbol}: {e}")
            return None
    
    try:
        # Run all tasks concurrently
        ticker_tasks = [fetch_ticker_data(ticker) for ticker in unique_tickers]
        quote_tasks = [fetch_quote_data(item) for item in result]
        
        ticker_results = await asyncio.gather(*ticker_tasks)
        quote_results = await asyncio.gather(*quote_tasks)
        
        # Process results
        combined_results = [res for res in quote_results if res]
        combined_news = [news_item for news, _ in ticker_results for news_item in news]
        combined_earnings = [earnings for _, earnings in ticker_results if earnings]
        
        # Final response structure
        res = {
            'data': combined_results,
            'news': combined_news,
            'earnings': combined_earnings,
        }
        
        # Serialize and compress the response data
        res_serialized = orjson.dumps(res)
        compressed_data = gzip.compress(res_serialized)

        return StreamingResponse(
            io.BytesIO(compressed_data),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")



def process_option_activity(item):
    item['put_call'] = 'Calls' if item['put_call'] == 'CALL' else 'Puts'
    item['underlying_type'] = item['underlying_type'].lower()
    item['price'] = round(float(item['price']), 2)
    item['strike_price'] = round(float(item['strike_price']), 2)
    item['cost_basis'] = round(float(item['cost_basis']), 2)
    item['underlying_price'] = round(float(item['underlying_price']), 2)
    item['option_activity_type'] = item['option_activity_type'].capitalize()
    item['sentiment'] = item['sentiment'].capitalize()
    item['execution_estimate'] = item['execution_estimate'].replace('_', ' ').title()
    item['tradeCount'] = item.get('trade_count', 0)
    return item

async def fetch_option_data(option_id: str):
    url = "https://api.benzinga.com/api/v1/signal/option_activity"
    headers = {"accept": "application/json"}
    querystring = {"token": Benzinga_API_KEY, "parameters[id]": option_id}
    
    try:
        response = requests.get(url, headers=headers, params=querystring)
        response.raise_for_status()
        data = orjson.loads(response.text)
        option_activity = data.get('option_activity', [])
        
        if isinstance(option_activity, list):
            return [process_option_activity(item) for item in option_activity]
        else:
            print(f"Unexpected response format for {option_id}: {option_activity}")
            return []
    except Exception as e:
        print(f"Error fetching data for {option_id}: {e}")
        return []

@app.post("/get-options-watchlist")
async def get_options_watchlist(data: OptionsWatchList, api_key: str = Security(get_api_key)):
    options_list_id = sorted(data.optionsIdList)
    cache_key = f"options-watchlist-{options_list_id}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    result = []

    for option_id in options_list_id:
        file_path = OPTIONS_WATCHLIST_DIR / f"{option_id}.json"
        
        if file_path.exists():
            with open(file_path, 'rb') as json_file:
                option_data = orjson.loads(json_file.read())
                result.extend(option_data)
        else:
            option_activity = await fetch_option_data(option_id)
            if option_activity:
                with open(file_path, 'wb') as file:
                    file.write(orjson.dumps(option_activity))
                result.extend(option_activity)

    compressed_data = gzip.compress(orjson.dumps(result))
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60 * 30)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/price-prediction")
async def brownian_motion(data:TickerData, api_key: str = Security(get_api_key)):

    data= data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"price-prediction-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    
    if ticker in etf_symbols:
        table_name = 'etfs'
    else:
        table_name = 'stocks'
    
    query = f"""
    SELECT 
        pricePrediction
    FROM 
        {table_name}
    WHERE
        symbol = ?
    """

    df = pd.read_sql_query(query, etf_con if table_name == 'etfs' else con, params=(ticker,))
    df_dict = df.to_dict()
    try:
        price_dict = orjson.loads(df_dict['pricePrediction'][0])
    except:
        price_dict = {'1W': {'min': 0, 'mean': 0, 'max': 0}, '1M': {'min': 0, 'mean': 0, 'max': 0}, '3M': {'min': 0, 'mean': 0, 'max': 0}, '6M': {'min': 0, 'mean': 0, 'max': 0}}

    redis_client.set(cache_key, orjson.dumps(price_dict))
    redis_client.expire(cache_key, 3600*24) # Set cache expiration time to 1 hour
    return price_dict



@app.post("/stock-screener-data")
async def stock_finder(data:StockScreenerData, api_key: str = Security(get_api_key)):
    rule_of_list = sorted(data.ruleOfList)

    cache_key = f"stock-screener-data-{rule_of_list}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    always_include = ['symbol', 'marketCap', 'price', 'changesPercentage', 'name','volume','pe']

    try:
        filtered_data = [
            {key: item.get(key) for key in set(always_include + rule_of_list) if key in item}
            for item in stock_screener_data
        ]
    except Exception as e:
        filtered_data = []

    # Compress the JSON data
    res = orjson.dumps(filtered_data)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/get-quant-stats")
async def get_quant_stats(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()
    cache_key = f"get-quant-stats-{ticker}"
    
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    if ticker in etf_symbols:
        table_name = 'etfs'
        query_con = etf_con
    elif ticker in crypto_symbols:
        table_name = 'cryptos'
        query_con = crypto_con
    else:
        table_name = 'stocks'
        query_con = con
    # If the hash doesn't exist or doesn't match, fetch data from the database
    query_metrics_template = f"""
        SELECT
            quantStats
        FROM
            {table_name}
        WHERE
            symbol = ?
    """

    metrics_data = pd.read_sql_query(query_metrics_template, query_con, params=(ticker,))
    
    try:
        #metrics_data = orjson.loads(metrics_data.to_dict()['quantStats'][0])
        metrics_data = metrics_data.to_dict()['quantStats'][0]
        metrics_data = eval(metrics_data)
    except:
        metrics_data = {}
    # Store the data and hash in the cache
    redis_client.set(cache_key, orjson.dumps(metrics_data))
    redis_client.expire(cache_key, 3600 *24) # Set cache expiration time to 1 hour

    return metrics_data





@app.post("/congress-trading-ticker")
async def get_fair_price(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"get-congress-trading-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/congress-trading/company/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 15*60) # Set cache expiration time to Infinity

    return res



@app.post("/shareholders")
async def get_shareholders(data: TickerData, api_key: str = Security(get_api_key)):

    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"shareholders-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/shareholders/{ticker}.json", 'rb') as file:
            shareholder_list = orjson.loads(file.read())
    except:
        shareholder_list = []

    try:
        with open(f"json/ownership-stats/{ticker}.json", 'rb') as file:
            stats = orjson.loads(file.read())
    except:
        stats = {}

    try:
        res = {**stats, 'shareholders': shareholder_list}
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res


@app.post("/cik-data")
async def get_hedge_funds_data(data: GetCIKData, api_key: str = Security(get_api_key)):
    data = data.dict()
    cik = data['cik']

    cache_key = f"{cik}-hedge-funds"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    
    try:
        with open(f"json/hedge-funds/companies/{cik}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 3600) # Set cache expiration time to Infinity

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/all-hedge-funds")
async def get_all_hedge_funds_data(api_key: str = Security(get_api_key)):
    
    cache_key = f"all-hedge-funds"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/hedge-funds/all-hedge-funds.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    res = orjson.dumps(res)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 3600) # Set cache expiration time to Infinity

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/searchbar")
async def get_stock(
    query: str = Query(""),
    api_key: str = Security(lambda: None)  # Replace with your actual security function
) -> JSONResponse:

    if not query:
        return JSONResponse(content=[])

    # Check for exact ISIN match first
    exact_match = next((item for item in searchbar_data if item.get("isin",None) == query), None)
    if exact_match:
        return JSONResponse(content=[exact_match])

    # Precompile case-insensitive regex for faster matching
    search_pattern = re.compile(re.escape(query.lower()), re.IGNORECASE)

    # Filter items based on the search pattern (ignore items where neither name nor symbol match)
    filtered_data = [
        item for item in searchbar_data
        if search_pattern.search(item['name']) or search_pattern.search(item['symbol'])
    ]

    # Sort by the calculated score, giving exact symbol matches the highest priority,
    # and then by descending marketCap for other matches
    results = sorted(
        filtered_data,
        key=lambda item: (
            calculate_score(item, query),
            0 if item.get('marketCap') is None else -item['marketCap']
        )
    )[:5]
    return JSONResponse(content=orjson.loads(orjson.dumps(results)))


@app.get("/full-searchbar")
async def get_data(api_key: str = Security(get_api_key)):
    
    cache_key = f"full-searchbar"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )


    res = orjson.dumps(searchbar_data)
    compressed_data = gzip.compress(res)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 3600) # Set cache expiration time to Infinity

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/revenue-segmentation")
async def revenue_segmentation(data: TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"revenue-segmentation-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        #redis_client.expire(cache_key, caching_time) 
        return orjson.loads(cached_result)


    query_template = """
        SELECT 
            revenue_product_segmentation, revenue_geographic_segmentation
        FROM 
            stocks 
        WHERE
            symbol = ?
    """

    query = query_template.format(ticker=ticker)
    cur = con.cursor()
    cur.execute(query, (ticker,))
    result = cur.fetchone()  # Get the first row

    if result is not None:
        product_list = orjson.loads(result[0])
        geographic_list = orjson.loads(result[1])
    else:
        product_list = []
        geographic_list = []

    res_list = [product_list, geographic_list]

    redis_client.set(cache_key, orjson.dumps(res_list))
    redis_client.expire(cache_key, 3600 * 24) # Set cache expiration time to Infinity

    return res_list



@app.post("/crypto-profile")
async def get_crypto_profile(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"crypto-profile-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    query_template = """
        SELECT 
            profile
        FROM 
            cryptos
        WHERE
            symbol = ?
    """
    query = query_template.format(ticker=ticker)
    cur = crypto_con.cursor()
    cur.execute(query, (ticker,))
    result = cur.fetchone()  # Get the first row
    profile_list = []

    try:
        if result is not None:
            profile_list = orjson.loads(result[0])
    except:
        profile_list = []

    redis_client.set(cache_key, orjson.dumps(profile_list))
    redis_client.expire(cache_key, 3600 * 24) # Set cache expiration time to Infinity

    return profile_list


@app.post("/etf-profile")
async def get_fair_price(data: TickerData, api_key: str = Security(get_api_key)):

    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"etf-profile-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    query_template = """
        SELECT 
            profile, etfProvider
        FROM 
            etfs
        WHERE
            symbol = ?
    """
    query = query_template.format(ticker=ticker)
    cur = etf_con.cursor()
    cur.execute(query, (ticker,))
    result = cur.fetchone()  # Get the first row
    profile_list = []

    try:
        if result is not None:
            profile_list = orjson.loads(result[0])
            for item in profile_list:
                item['etfProvider'] = result[1]
            #Show only hedge funds that are in the institute.db
    except:
        profile_list = []

    redis_client.set(cache_key, orjson.dumps(profile_list))
    redis_client.expire(cache_key, 3600 * 24) # Set cache expiration time to Infinity

    return profile_list



@app.post("/etf-holdings")
async def etf_holdings(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"etf-holdings-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/etf/holding/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60*10)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/etf-sector-weighting")
async def etf_holdings(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"etf-sector-weighting-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/etf-sector/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.get("/all-etf-tickers")
async def get_all_etf_tickers(api_key: str = Security(get_api_key)):
    cache_key = f"all-etf-tickers"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/all-symbols/etfs.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    # Compress the JSON data
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/all-crypto-tickers")
async def get_all_crypto_tickers(api_key: str = Security(get_api_key)):
    cache_key = f"all-crypto-tickers"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/all-symbols/cryptos.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    # Compress the JSON data
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/congress-rss-feed")
async def get_congress_rss_feed(api_key: str = Security(get_api_key)):
    cache_key = f"congress-rss-feed"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/congress-trading/rss-feed/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*15)
    return res




@app.post("/historical-sector-price")
async def historical_sector_price(data:FilterStockList, api_key: str = Security(get_api_key)):
    data = data.dict()
    sector = data['filterList']
    cache_key = f"history-price-sector-{sector}"
    cached_result = redis_client.get(cache_key)

    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/sector/{sector}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*60)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



def remove_text_before_operator(text):
    # Find the index of the first occurrence of "Operator"
    operator_index = text.find("Operator")

    # If "Operator" was found, create a new string starting from that index
    if operator_index != -1:
        new_text = text[operator_index:]
        return new_text
    else:
        return "Operator not found in the text."



def extract_names_and_descriptions(text):
    pattern = r'([A-Z][a-zA-Z\s]+):\s+(.*?)(?=\n[A-Z][a-zA-Z\s]+:|$)'
    matches = re.findall(pattern, text, re.DOTALL)
    extracted_data = []
    
    for match in matches:
        name = match[0].strip()
        description = match[1].strip()
        
        # Split the description into sentences
        sentences = re.split(r'(?<=[.!?])\s+', description)
        
        # Add line breaks every 3 sentences
        formatted_description = ""
        for i, sentence in enumerate(sentences, 1):
            formatted_description += sentence + " "
            if i % 3 == 0:
                formatted_description += "<br><br>"
        
        formatted_description = formatted_description.strip()
        
        extracted_data.append({'name': name, 'description': formatted_description})
    
    return extracted_data


@app.post("/earnings-call-transcripts")
async def get_earnings_call_transcripts(data:TranscriptData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker']
    year = data['year']
    quarter = data['quarter']
    cache_key = f"earnings-call-transcripts-{ticker}-{year}-{quarter}"
    cached_result = redis_client.get(cache_key)

    if cached_result:
        return orjson.loads(cached_result)
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://financialmodelingprep.com/api/v3/earning_call_transcript/{ticker}?year={year}&quarter={quarter}&apikey={FMP_API_KEY}"

            async with session.get(url) as response:
                data = (await response.json())[0]
        

        content = remove_text_before_operator(data['content'])
        chat = extract_names_and_descriptions(content)
        res = {'date': data['date'], 'chat': chat}
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res

@app.get("/ticker-mentioning")
async def get_ticker_mentioning(api_key: str = Security(get_api_key)):

    cache_key = f"get-ticker-mentioning"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/ticker-mentioning/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res


@app.post("/top-etf-ticker-holder")
async def top_etf_ticker_holder(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"top-etf-{ticker}-holder"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/top-etf-ticker-holder/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*24)  # Set cache expiration time to 1 day
    return res


@app.get("/popular-etfs")
async def get_popular_etfs(api_key: str = Security(get_api_key)):
    cache_key = "popular-etfs"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open("json/mini-plots-index/data.json", 'rb') as file:
            res = orjson.loads(file.read())
            for item in res:
                price_data = item["priceData"]
                last_price_data = price_data[-1]  # Get the last element of priceData
                if last_price_data['value'] == None:
                    last_price_data = price_data[-2]  # If last element is None, take the second last
                
                item["price"] = last_price_data["value"]  # Update priceData with just the value
                del item["priceData"]  # Remove the old key
                del item["previousClose"]  # Remove the old key
    except Exception as e:
        print(f"Error: {e}")
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*5)  # Set cache expiration time to 5 minutes
    return res


@app.get("/all-etf-providers")
async def get_all_etf_providers(api_key: str = Security(get_api_key)):

    cache_key = f"get-all-etf-providers"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/all-etf-providers/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res



@app.post("/etf-provider")
async def etf_holdings(data: ETFProviderData, api_key: str = Security(get_api_key)):
    etf_provider = data.etfProvider.lower()
    cache_key = f"etf-provider-{etf_provider}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/etf/provider/{etf_provider}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60*10)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/etf-new-launches")
async def etf_provider(api_key: str = Security(get_api_key)):
    cache_key = f"etf-new-launches"
    cached_result = redis_client.get(cache_key)
    limit = 100
    if cached_result:
        return orjson.loads(cached_result)

    # Check if data is cached; if not, fetch and cache it
    cursor = etf_con.cursor()
    query = "SELECT symbol, name, expenseRatio, totalAssets, numberOfHoldings, inceptionDate FROM etfs ORDER BY inceptionDate DESC LIMIT ?"
    cursor.execute(query, (limit,))
    raw_data = cursor.fetchall()
    cursor.close()

    # Extract only relevant data and sort it
    res = [{'symbol': row[0], 'name': row[1], 'expenseRatio': row[2], 'totalAssets': row[3], 'numberOfHoldings': row[4], 'inceptionDate': row[5]} for row in raw_data]
    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res

@app.get("/etf-bitcoin-list")
async def get_etf_bitcoin_list(api_key: str = Security(get_api_key)):

    cache_key = f"get-etf-bitcoin-list"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/etf-bitcoin-list/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []
    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res


@app.post("/analyst-estimate")
async def get_analyst_estimate(data:TickerData, api_key: str = Security(get_api_key)):
    data = data.dict()
    ticker = data['ticker'].upper()

    cache_key = f"get-analyst-estimates-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/analyst-estimate/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res


@app.post("/insider-trading")
async def get_insider_trading(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"insider-trading-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/insider-trading/history/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    # Compress the JSON data
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

    

@app.post("/insider-trading-statistics")
async def get_insider_trading_statistics(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"insider-trading-statistics-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/insider-trading/statistics/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())[0]
    except:
        res = {}
    
    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res

@app.post("/get-executives")
async def get_executives(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"get-executives-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/executives/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day
    return res

@app.post("/get-sec-filings")
async def get_sec_filings(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"get-sec-filings-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/sec-filings/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    # Compress the JSON data
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/ipo-calendar")
async def get_ipo_calendar(data:IPOData, api_key: str = Security(get_api_key)):
    year = data.year
    cache_key = f"ipo-calendar-{year}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/ipo-calendar/data.json", 'rb') as file:
            res = orjson.loads(file.read())
        if year != 'all':
            res = [entry for entry in res if entry['date'].startswith(year)]
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600 * 24)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/trending")
async def get_trending(api_key: str = Security(get_api_key)):
    cache_key = f"get-trending"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/trending/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*15)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/heatmap")
async def get_heatmap(api_key: str = Security(get_api_key)):
    cache_key = "heatmap"
    cached_result = redis_client.get(cache_key)
    
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="text/html",
            headers={"Content-Encoding": "gzip"}
        )
    
    try:
        with open("json/heatmap/data.html", 'r', encoding='utf-8') as file:
            html_content = file.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Heatmap file not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading heatmap file: {str(e)}")
    
    # Compress the HTML content
    compressed_data = gzip.compress(html_content.encode('utf-8'))
    
    # Cache the compressed HTML
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60 * 5)  # Set cache expiration time to 5 min
    
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="text/html",
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "public, max-age=300"  # 5 minutes cache
        }
    )

@app.post("/pre-post-quote")
async def get_pre_post_quote(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"get-pre-post-quote-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/pre-post-quote/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60)  # Set cache expiration time to 1 day
    return res

@app.post("/get-quote")
async def get_pre_post_quote(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"get-quote-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/quote/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60)  # Set cache expiration time to 1 day
    return res



@app.post("/options-contract-history")
async def get_data(data:GeneralData, api_key: str = Security(get_api_key)):
    contract_id = data.params
    cache_key = f"options-contract-history-{contract_id}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/hottest-contracts/contracts/{contract_id}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*60)
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/options-gex-dex")
async def get_data(data:ParamsData, api_key: str = Security(get_api_key)):
    ticker = data.params.upper()
    category = data.category.lower()

    cache_key = f"options-gex-dex-{ticker}-{category}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/gex-dex/{category}/{ticker}.json", 'rb') as file:
            data = orjson.loads(file.read())
            if category == 'strike':
                key_element = 'gex'
                val_sums = [item[f"call_{key_element}"] + item[f"put_{key_element}"] for item in data]
                threshold = np.percentile(val_sums, 85)
                data = [item for item in data if (item[f"call_{key_element}"] + item[f"put_{key_element}"]) >= threshold]

    except:
        data = []
    data = orjson.dumps(data)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*60)
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/options-oi")
async def get_data(data:ParamsData, api_key: str = Security(get_api_key)):
    ticker = data.params.upper()
    category = data.category.lower()

    cache_key = f"options-oi-{ticker}-{category}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})
    try:
        with open(f"json/oi/{category}/{ticker}.json", 'rb') as file:
            data = orjson.loads(file.read())
            if category == 'strike':
                val_sums = [item[f"call_oi"] + item[f"put_oi"] for item in data]
                threshold = np.percentile(val_sums, 85)
                data = [item for item in data if (item[f"call_oi"] + item[f"put_oi"]) >= threshold]     
    except:
        data = []
    data = orjson.dumps(data)

    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*60)
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/options-stats-ticker")
async def get_options_stats_ticker(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"options-stats-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/options-stats/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*5)
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/raw-options-flow-ticker")
@limiter.limit("500/minute")
async def get_raw_options_flow_ticker(data:OptionsFlowData, request: Request, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    start_date = data.start_date
    end_date = data.end_date
    pagesize = data.pagesize
    page = data.page
    cache_key = f"raw-options-flow-{ticker}-{start_date}-{end_date}-{pagesize}-{page}"
    #print(ticker, start_date, end_date, pagesize, page)
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})
    try:
        data = fin.options_activity(company_tickers=ticker, date_from=start_date, date_to = end_date, page=page, pagesize=pagesize)
        data = orjson.loads(fin.output(data))['option_activity']
    except Exception as e:
        print(e)
        data = []

    data = orjson.dumps(data)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60)  # Set cache expiration time to 5 min

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/options-flow-ticker")
async def get_options_flow_ticker(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"options-flow-{ticker}"

    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})
    try:
        data = fin.options_activity(company_tickers=ticker, pagesize=500)
        data = orjson.loads(fin.output(data))['option_activity']
        res_list = []
        keys_to_keep = {'time', 'sentiment','option_activity_type', 'price', 'underlying_price', 'cost_basis', 'strike_price', 'date', 'date_expiration', 'open_interest', 'put_call', 'volume'}
        for item in data:
            filtered_item = {key: value for key, value in item.items() if key in keys_to_keep}
            filtered_item['type'] = filtered_item['option_activity_type'].capitalize()
            filtered_item['sentiment'] = filtered_item['sentiment'].capitalize()
            filtered_item['underlying_price'] = round(float(filtered_item['underlying_price']),2)
            #filtered_item['time'] = (datetime.strptime(filtered_item['time'], '%H:%M:%S')-timedelta(hours=0)).strftime('%H:%M:%S')
            filtered_item['put_call'] = 'Calls' if filtered_item['put_call'] == 'CALL' else 'Puts'
            res_list.append(filtered_item)
    except:
        res_list = []

    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*5)  # Set cache expiration time to 5 min

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/options-gex-ticker")
async def get_options_flow_ticker(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"options-gex-{ticker}"

    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})
    try:
        with open(f"json/options-gex/companies/{ticker}.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = []

    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 5 min

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/options-historical-data-ticker")
async def get_options_chain(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"options-historical-data-{ticker}"

    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})
    try:
        with open(f"json/options-historical-data/companies/{ticker}.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = []

    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 5 min

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/options-historical-flow")
async def get_options_chain(data:HistoricalDate, api_key: str = Security(get_api_key)):
    selected_date = data.date
    cache_key = f"options-historical-flow-{selected_date}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})
    try:
        with open(f"json/options-historical-data/flow-data/{selected_date}.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = []
    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 5 min

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

'''
@app.post("/options-flow-feed")
async def get_options_flow_feed(data: LastOptionId, api_key: str = Security(get_api_key)):
    last_option_id = data.lastId

    try:
        with open(f"json/options-flow/feed/data.json", 'rb') as file:
            all_data = orjson.loads(file.read())

        if len(last_option_id) == 0:
            res_list = all_data[0:100]
        else:
            # Find the index of the element with the last known ID
            start_index = next((i for i, item in enumerate(all_data) if item["id"] == last_option_id), -1)
            if start_index == -1:
                raise ValueError("Last known ID not found in data")

            # Get the next 100 elements
            res_list = all_data[start_index + 1:start_index + 101]

        # Compress the data
        compressed_data = gzip.compress(orjson.dumps(res_list))

        return StreamingResponse(
            io.BytesIO(compressed_data),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    except Exception as e:
        # Log the error for debugging
        print(f"Error: {str(e)}")
        return StreamingResponse(
            io.BytesIO(gzip.compress(orjson.dumps([]))),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
'''
@app.get("/options-flow-feed")
async def get_options_flow_feed(api_key: str = Security(get_api_key)):
    try:
        with open(f"json/options-flow/feed/data.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = []
    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/dark-pool-flow-feed")
async def get_dark_pool_feed(api_key: str = Security(get_api_key)):
    cache_key = f"dark-pooll-flow-feed"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        directory = "json/dark-pool/historical-flow"
        res_list = load_latest_json(directory)
        res_list = [item for item in res_list if float(item['premium']) > 500_000]

    except:
        res_list = []
        
    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.get("/options-zero-dte")
async def get_options_flow_feed(api_key: str = Security(get_api_key)):
    try:
        with open(f"json/options-flow/zero-dte/data.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = []
    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/options-bubble")
async def get_options_bubble(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"options-bubble-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/options-bubble/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*24)  # Set cache expiration time to 1 day
    return res


@app.get("/top-analysts")
async def get_all_analysts(api_key: str = Security(get_api_key)):
    cache_key = f"top-analysts"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/analyst/top-analysts.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*60*2)  # Set cache expiration time to 1 day
    return res

@app.get("/top-analysts-stocks")
async def get_all_analysts(api_key: str = Security(get_api_key)):
    cache_key = f"top-analysts-stocks"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/analyst/top-stocks.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*60*2)  # Set cache expiration time to 1 day
    return res

@app.post("/analyst-stats")
async def get_all_analysts(data:AnalystId, api_key: str = Security(get_api_key)):
    analyst_id = data.analystId

    cache_key = f"analyst-stats-{analyst_id}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/analyst/analyst-db/{analyst_id}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*60*2)  # Set cache expiration time to 1 day
    return res

@app.post("/wiim")
async def get_wiim(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"wiim-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)

    try:
        with open(f"json/wiim/company/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())[:5]
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*5)
    return res

@app.get("/dashboard-info")
async def get_dashboard_info(api_key: str = Security(get_api_key)):

    cache_key = f"dashboard-info"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/dashboard/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*5)
    return res

@app.post("/sentiment-analysis")
async def get_sentiment_analysis(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"sentiment-analysis-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/sentiment-analysis/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res

@app.post("/trend-analysis")
async def get_trend_analysis(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"trend-analysis-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/trend-analysis/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res

@app.post("/price-analysis")
async def get_price_analysis(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"price-analysis-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/price-analysis/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/fundamental-predictor-analysis")
async def get_fundamental_predictor_analysis(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"fundamental-predictor-analysis-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/fundamental-predictor-analysis/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res


@app.post("/value-at-risk")
async def get_trend_analysis(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"value-at-risk-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/var/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res

@app.post("/government-contract")
async def get_government_contract(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"government-contract-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/government-contract/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res

@app.post("/corporate-lobbying")
async def get_lobbying(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"corporate-lobbying-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/corporate-lobbying/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res

@app.post("/enterprise-values")
async def get_enterprise_values(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"enterprise-values-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/enterprise-values/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res


@app.post("/share-statistics")
async def get_enterprise_values(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"share-statistics-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/share-statistics/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res


@app.post("/politician-stats")
async def get_politician_stats(data:PoliticianId, api_key: str = Security(get_api_key)):
    politician_id = data.politicianId.lower()
    cache_key = f"politician-stats-{politician_id}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/congress-trading/politician-db/{politician_id}.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = {}

    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/all-politicians")
async def get_all_politician(api_key: str = Security(get_api_key)):
    
    cache_key = f"all-politician"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    
    try:
        with open(f"json/congress-trading/search_list.json", 'rb') as file:
            res_list = orjson.loads(file.read())
    except:
        res_list = []

    data = orjson.dumps(res_list)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.get("/most-shorted-stocks")
async def get_most_shorted_stocks(api_key: str = Security(get_api_key)):
    cache_key = f"most-shorted-stocks"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/most-shorted-stocks/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res


@app.post("/historical-dark-pool")
async def get_dark_pool(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"historical-dark-pool-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/dark-pool/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/dark-pool-level")
async def get_dark_pool(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"dark-pool-level-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
        io.BytesIO(cached_result),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"})

    try:
        with open(f"json/dark-pool/price-level/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)
    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*5)
    
    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/market-maker")
async def get_market_maker(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"market-maker-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/market-maker/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res

@app.post("/clinical-trial")
async def get_clinical_trial(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"clinical-trial-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/clinical-trial/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/fda-calendar")
async def get_market_maker(api_key: str = Security(get_api_key)):
    cache_key = f"fda-calendar"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/fda-calendar/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 60*15)  # Set cache expiration time to 1 day
    return res


@app.post("/fail-to-deliver")
async def get_fail_to_deliver(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"fail-to-deliver-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/fail-to-deliver/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/analyst-insight")
async def get_analyst_insight(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"analyst-insight-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/analyst/insight/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key, 3600*3600)  # Set cache expiration time to 1 day
    return res


@app.post("/implied-volatility")
async def get_data(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"implied-volatility-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/implied-volatility/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except Exception as e:
        print(e)
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*60)  # Set cache expiration time to 1 day

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/hottest-contracts")
async def get_data(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"hottest-contracts-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/hottest-contracts/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*10)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/cramer-tracker")
async def get_cramer_tracker(api_key: str = Security(get_api_key)):
    cache_key = f"cramer-tracker"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/cramer-tracker/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/lobbying-tracker")
async def get_cramer_tracker(api_key: str = Security(get_api_key)):
    cache_key = f"corporate-lobbying-tracker"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/corporate-lobbying/tracker/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*15)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/reddit-tracker")
async def get_reddit_tracker(api_key: str = Security(get_api_key)):
    cache_key = f"reddit-tracker"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/reddit-tracker/wallstreetbets/data.json", 'rb') as file:
            latest_post = orjson.loads(file.read())[0:25]
    except:
        latest_post = []

    try:
        with open(f"json/reddit-tracker/wallstreetbets/stats.json", 'rb') as file:
            stats = orjson.loads(file.read())
    except:
        stats = []

    try:
        with open(f"json/reddit-tracker/wallstreetbets/trending.json", 'rb') as file:
            trending = orjson.loads(file.read())
    except:
        trending = {}

    res = {'posts': latest_post, 'stats': stats, 'trending': trending}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key, 60*15)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )



@app.post("/historical-market-cap")
async def get_historical_market_cap(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"historical-market-cap-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/market-cap/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/economic-indicator")
async def get_economic_indicator(api_key: str = Security(get_api_key)):
    cache_key = f"economic-indicator"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/economic-indicator/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/sector-industry-overview")
async def get_industry_overview(api_key: str = Security(get_api_key)):
    cache_key = f"sector-industry-overview"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/industry/overview.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/sector-overview")
async def get_sector_overview(api_key: str = Security(get_api_key)):
    cache_key = f"sector-overview"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/industry/sector-overview.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/industry-stocks")
async def get_sector_overview(data: FilterStockList, api_key: str = Security(get_api_key)):
    filter_list = data.filterList.lower()
    cache_key = f"industry-stocks-{filter_list}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/industry/industries/{filter_list}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60*15)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/industry-overview")
async def get_industry_overview(api_key: str = Security(get_api_key)):
    cache_key = f"industry-overview"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/industry/industry-overview.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/next-earnings")
async def get_next_earnings(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"next-earnings-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/earnings/next/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,15*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/earnings-surprise")
async def get_surprise_earnings(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"earnings-surprise-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/earnings/surprise/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,15*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/price-action-earnings")
async def get_data(data:TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"price-action-earnings-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/earnings/past/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/info-text")
async def get_info_text(data:InfoText, api_key: str = Security(get_api_key)):
    parameter = data.parameter
    cache_key = f"info-text-{parameter}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return orjson.loads(cached_result)
    try:
        with open(f"json/info-text/data.json", 'rb') as file:
            res = orjson.loads(file.read())[parameter]
    except:
        res = {}

    redis_client.set(cache_key, orjson.dumps(res))
    redis_client.expire(cache_key,3600*3600)

    return res

@app.post("/fomc-impact")
async def get_fomc_impact(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()

    cache_key = f"fomc-impact-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/fomc-impact/companies/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/sentiment-tracker")
async def get_fomc_impact(api_key: str = Security(get_api_key)):

    cache_key = f"sentiment-tracker"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/tracker/sentiment/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,5*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/business-metrics")
async def get_fomc_impact(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"business-metrics-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/business-metrics/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/insider-tracker")
async def get_insider_tracker(api_key: str = Security(get_api_key)):
    cache_key = f"insider-tracker"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/tracker/insider/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,5*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/statistics")
async def get_statistics(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"statistics-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    try:
        with open(f"json/statistics/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}

    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/list-category")
async def get_statistics(data: FilterStockList, api_key: str = Security(get_api_key)):
    filter_list = data.filterList.lower()
    cache_key = f"filter-list-{filter_list}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    if filter_list in ['financial','healthcare','technology','industrials','consumer-cyclical','real-estate','basic-materials','communication-services','energy','consumer-defensive','utilities']:
        category_type = 'sector'
    elif filter_list == 'reits':
        category_type = 'industry'
    elif filter_list in ['online-gambling','metaverse','sports-betting','virtual-reality','online-dating','pharmaceutical-stocks','gaming-stocks','augmented-reality','electric-vehicles','car-company-stocks','esports','clean-energy','mobile-games','social-media-stocks','ai-stocks','highest-option-premium','highest-option-iv-rank','highest-open-interest','highest-open-interest-change','most-shorted-stocks','most-ftd-shares','highest-income-tax','most-employees','highest-revenue','top-rated-dividend-stocks','penny-stocks','overbought-stocks','oversold-stocks','faang','magnificent-seven','ca','cn','de','gb','il','in','jp','nyse','nasdaq','amex','dowjones','sp500','nasdaq100','all-etf-tickers','all-stock-tickers']:
        category_type = 'stocks-list'
    elif filter_list in ['dividend-kings','dividend-aristocrats']:
        category_type = 'dividends'
    else:
        category_type = 'market-cap'
    try:
        with open(f"json/{category_type}/list/{filter_list}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60*10)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.post("/pre-after-market-movers")
async def get_statistics(data: ParamsData, api_key: str = Security(get_api_key)):
    params = data.params
    category = data.category
    cache_key = f"pre-after-market-movers-{category}-{params}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/market-movers/{category}/{params}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {'gainers': [], 'losers': []}
        
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,60*15)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.post("/profile")
async def get_statistics(data: TickerData, api_key: str = Security(get_api_key)):
    ticker = data.ticker.upper()
    cache_key = f"profile-{ticker}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/profile/{ticker}.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}
        
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,3600*3600)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )

@app.get("/market-flow")
async def get_market_flow(api_key: str = Security(get_api_key)):
    cache_key = f"market-flow"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return StreamingResponse(
            io.BytesIO(cached_result),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )

    try:
        with open(f"json/market-flow/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = {}
        
    data = orjson.dumps(res)
    compressed_data = gzip.compress(data)

    redis_client.set(cache_key, compressed_data)
    redis_client.expire(cache_key,2*60)

    return StreamingResponse(
        io.BytesIO(compressed_data),
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/newsletter")
async def get_newsletter():
    try:
        with open(f"json/newsletter/data.json", 'rb') as file:
            res = orjson.loads(file.read())
    except:
        res = []
    return res

if __name__ == "__main__":
    api_port = int(os.environ.get('API_PORT', DEFAULT_UVICORN_PORT))
    uvicorn_base_url = os.environ.get('UVICORN_BASE_URL', DEFAULT_UVICORN_BASE_URL)
    uvicorn.run(app, host=uvicorn_base_url, port=api_port)
