# prepare data
```
config in research/.env:
python3 research/aggregate_ohlcv.py
python3 research/fetch_tweets.py
```

# run
```
cd backend && source .venv/bin/activate && python run.py
python3 research/backtest_pipeline.py
```