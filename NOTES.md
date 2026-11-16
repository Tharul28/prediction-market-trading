# Learning notes

These notes are for defending this project in an interview. Part 1 explains every concept the code uses. Part 2 walks through the code from raw data to results. Part 3 lists questions an interviewer at a trading firm might ask, with notes on how to think about each one rather than answers to memorise. Part 4 lists improvements to build yourself, ordered by difficulty.

A good habit while reading: for every claim, find the line of code that implements it, and for every number in the README, know which file it came from.

---

## Part 1: Concepts

### Markets and prices

**Binary contract.** Pays $1 (100 cents) if an event happens and $0 if not. On Kalshi each outcome of an event ("who wins?", "which range will the number fall in?") is its own binary market.

**Order book.** The list of resting limit orders in one market. Bids are offers to buy, asks (offers) are offers to sell. Each price level has a total size. The book in this project is `OrderBookSnapshot` in `orderbook/book.py`.

**Best bid, best ask, the touch.** The highest bid and the lowest ask. Together they are "the touch" or "top of book".

**Spread.** Best ask minus best bid. It is the cost of trading immediately: buying at the ask and selling straight back at the bid loses the spread. It is also what a market maker hopes to earn.

**Mid price.** Average of best bid and best ask. A simple estimate of value, but not a price anyone can trade at.

**Microprice.** Mid price weighted by the size on each side: `(bid x ask_size + ask x bid_size) / (bid_size + ask_size)`. If there are 90 contracts bid and 10 offered, the offer is likely to be taken soon, so the "true" price is probably closer to the ask. The microprice reflects that; the mid does not.

**Implied probability.** For a $1 binary contract, a price of 37 cents implies a probability of about 0.37. "About" because the price also includes fees, the time value of money (you lock up cash until settlement) and any risk premium. On Betfair, prices are decimal odds; the implied probability is 1 / odds (`decimal_odds_to_probability`).

**Overround.** For a set of outcomes where exactly one must happen, the implied probabilities should add up to 1. If the prices you can *buy* at add up to more than 1, the excess is the overround: the market's built-in margin. If they add up to less than 1 (or the prices you can *sell* at add up to more than 1), there may be an arbitrage.

**Depth.** How much size sits in the book near the touch. Deep books absorb large orders without the price moving much.

**Order book imbalance.** `(bid depth - ask depth) / (bid depth + ask depth)`. Positive means more buying interest. In many markets it has weak short-term predictive power for the next price move.

**Mutually exclusive and exhaustive.** Mutually exclusive: at most one outcome can happen. Exhaustive: at least one must happen. Both together: exactly one happens. The "buy every outcome" arbitrage needs both; "sell every outcome" needs only mutual exclusivity.

**Short positions.** Selling a YES contract you do not own. On Kalshi you do this by buying NO at 100 minus the price. One YES plus one NO always pays exactly 100, so being short YES at price p has the same profit and loss as being long NO at 100 - p. The difference is cash: the short YES "receives" p in this project's accounting, while on Kalshi you would pay 100 - p for the NO. The paper trader's balance check uses the Kalshi version (`BacktestEngine.available_balance`).

### Trading mechanics

**Limit order, market order.** A limit order says "buy up to this price". A market order says "buy now at whatever price". This project uses limit orders only; a taker order is a limit order that crosses the spread.

**Maker and taker.** A maker's order rests on the book and is filled later by someone else. A taker's order trades immediately against resting orders. Exchanges usually charge takers more, because makers provide the liquidity.

**Post-only.** A limit order that is rejected rather than allowed to trade immediately. Market makers use it to make sure they are always the maker. `PlaceLimit` is post-only in this project.

**Immediate-or-cancel (IOC).** Trade whatever you can right now, cancel the rest. `PlaceTaker` is IOC.

**Queue position and price-time priority.** At one price, orders are filled in the order they arrived. If 300 contracts are bid at 50 and you join with 10, the next 300 contracts sold at 50 go to the people ahead of you. This is why the same strategy can be profitable or not depending on whether you assume you are first in the queue.

**Latency.** Time between seeing information and your order reaching the exchange. During that time prices can move and your stale quotes can be traded against.

**Fees.** Kalshi's taker fee is `0.07 x C x P x (1 - P)` rounded up to the next cent, where C is contracts and P the price in dollars. The `P(1 - P)` term is the variance of a binary outcome with probability P, so the fee is highest at 50 cents (1.75 cents per contract) and small near 0 or 100. Some markets also charge makers at a quarter of that rate.

### Market making and its risks

**Market making.** Quoting both a bid and an ask, hoping to buy at the bid and sell at the ask and earn the spread. The market maker provides liquidity and is paid for it, but takes two risks.

**Inventory risk.** If you buy more than you sell, you hold a position, and the price can move against it. A market maker wants to earn the spread, not to bet on direction.

**Inventory skew.** Shifting both quotes against your position: when long, lower the bid (buy less) and lower the ask (sell more). In the Avellaneda-Stoikov model the optimal quotes are centred on a "reservation price" equal to the mid minus a term proportional to inventory, risk aversion and volatility. This project uses the simplest linear version.

**Adverse selection.** The traders who choose to trade with you are not random. If someone knows the price is about to fall, they will happily sell to your bid. On average, the fills you get are the ones you would rather not have. Market makers lose to informed traders and win from uninformed ones; the spread has to cover the losses to the first group.

**Markout.** A way to measure adverse selection. For each fill, record how far the mid moved in your favour after h seconds. A buy at 50 followed by a mid of 52 a minute later is a +2 markout. Consistently negative markouts mean you are being picked off. Markouts at horizon 0 show how much better than mid you traded (spread capture). Computing them uses future prices, but only as an after-the-fact evaluation; nothing feeds back into the strategy, so this is not lookahead.

**Fill model.** In a backtest, your orders were never in the historical book, so you must decide when they would have been filled. Optimistic fill models (always first in the queue, filled whenever the price touches you) are the most common way backtests of market-making strategies lie.

**Legging risk.** In a trade with several legs (like buying all three outcomes), the risk that some legs fill and others do not, leaving you with an unhedged position.

### Arbitrage

**Arbitrage.** A set of trades with a guaranteed non-negative payoff and a positive payoff in some cases, for no net cost. In a multi-outcome event, if you can buy one YES of every outcome for less than 100 cents in total (including fees), and exactly one must win, you are guaranteed 100 at settlement. In practice, "risk free" depends on every leg filling at the price you saw, and on the event rules really being mutually exclusive and exhaustive.

### Measuring performance

**Gross and net P&L.** Before and after fees and transaction costs.

**Mark-to-market.** Valuing open positions at a current price (here the mid). Optimistic for large positions, because you cannot actually sell at the mid.

**Liquidation value.** Valuing positions at the price you would get by closing them now (longs at the bid, shorts at the ask).

**Settlement value.** What the positions pay once the event resolves. For a basket of complete sets this is certain; for unhedged positions it is a coin flip with large variance.

**Volatility of returns.** Here, the standard deviation of P&L over 1-minute intervals. There is no natural capital base for a market-making account, so the project works in dollars of P&L rather than percentage returns.

**Drawdown and maximum drawdown.** The fall in equity from its previous peak, and the largest such fall. It measures the pain of holding the strategy and is often what gets a strategy switched off.

**Sharpe ratio.** Mean return divided by standard deviation of returns, usually annualised by multiplying by the square root of the number of periods per year. Here it is not annualised: annualising a two-hour simulation would multiply a noisy number by a large constant. The cross-session version (mean session P&L / std of session P&L) is the more honest one for this project.

**Fill rate.** Share of the contracts you quoted that actually traded. Low fill rates are normal for passive strategies.

### Backtesting pitfalls

**Event-driven backtest.** Replaying the data one event at a time, in time order, so that at every step the strategy only knows the past. The alternative, vectorised backtesting (compute signals on the whole data set at once), is faster but makes it easy to use information from the future by accident, for example by computing a signal with a centred rolling window.

**Lookahead bias.** Using information in a backtest that would not have been available at the time. Examples: trading at the price that triggered the signal, normalising with statistics of the whole data set, using a settlement result, or letting a simulator's true probability leak into the strategy. This project prevents it by construction (the strategy only receives a `StrategyContext`) and by tests.

**Overfitting.** Choosing parameters (or whole strategies) because they did well on the data you tested them on. With enough parameters and attempts, something always looks good in-sample. Defences: fewer parameters, out-of-sample tests (fit on one period, test on a later one), walk-forward testing, counting how many things you tried, and asking whether the result makes economic sense.

**Multiple testing.** If you try 20 strategies with no real edge, one will look significant at the 5% level by chance. The half-spread sweep in this project is four tries; that is why it is labelled "illustration only".

**Simulation versus reality.** A simulator only contains the effects you put into it. Results on simulated data are best used to compare variants of a strategy under known assumptions, not to estimate real profits.

### The statistics used by the simulator

**Random walk.** Each step adds an independent random increment. Its best forecast of the future value is the current value (a martingale), so it has no drift. The true probabilities here are a random walk in "logit" space, mapped back to probabilities with a softmax so they stay in (0, 1) and sum to 1.

**Poisson process.** Events arriving independently at a constant average rate. The number of events in a time window has a Poisson distribution; the gaps between them are exponential. Used for trade arrivals and news jumps.

**AR(1) process.** `x[t] = phi x[t-1] + noise`. With phi just below 1, it is persistent but always pulled back towards zero. Used for quoting errors and size noise, so that mispricings last a while and then fade.

---

## Part 2: How the code flows, from raw data to results

1. **Getting data.** Either `data/simulator.py` generates a session, or `data/kalshi.py` collects one. Both produce a `MarketData` object (`data/schema.py`) holding two long tables, `books` (one row per price level per snapshot) and `trades` (one row per trade, with the aggressor side), plus the list of markets and whether they are mutually exclusive and exhaustive. The simulator also attaches the true probability path (`truth`), used only for charts. `data/loader.py` is the single entry point: `load_market_data("simulated", seed=...)` or `load_market_data("data/raw/myevent")`.
2. **Splitting into sessions.** `split_into_sessions` cuts a long recording into 2-hour windows so it can be compared with simulated sessions of the same length.
3. **Order book metrics.** `snapshots_from_books` turns the long table into `OrderBookSnapshot` objects; `orderbook/metrics.py` computes spread, mid, microprice, depth, imbalance for each one. These feed the charts and the markout calculation.
4. **The event stream.** `backtest/events.py` merges snapshots and trades into one list sorted by time, with trades before books at equal times.
5. **The engine loop.** `BacktestEngine.run` calls `process(event)` for each event. Inside: apply due cancels, activate due orders (setting their queue position with the fill model, rejecting post-only orders that would cross), then either match a trade against our resting orders (`FillModel.match_trade`) or handle a snapshot: update the book, check resting orders for crosses (`FillModel.update_on_book`), execute due taker orders against the snapshot, call the strategy, record equity.
6. **The strategy.** `on_book_update(market_id, context)` receives the latest books, positions and its own open orders, and returns `PlaceLimit`, `PlaceTaker` and `Cancel` actions. The engine checks each one: market exists, price in range, position limit (counting all live orders on that side), balance (if set). Accepted orders become pending until `submitted_at + latency`.
7. **Fills and accounting.** Every fill goes through `_fill`: fee from `FeeModel`, then `Portfolio.apply_fill` updates cash, position and fees, and the strategy's `on_fill` is called. Equity is cash plus positions marked at the latest mid.
8. **Metrics.** `backtest/metrics.py` turns a `BacktestResult` (fills, orders, equity table) into the summary numbers, markouts and basket statistics.
9. **Scenarios and reports.** `scripts/run_all.py` defines the scenarios (strategy plus engine settings), runs each on every session, writes `runs.csv`, `scenario_summary.csv`, markouts, fill reasons and the sweep, and draws the charts with `scripts/plotting.py`. With `--compare` it runs simulated and real data and writes a side-by-side table.
10. **Paper trading.** `paper/trader.py` creates the same engine with no events, calls `reset()`, and then on every poll calls `process()` with the snapshots and new trades it just fetched. After each poll it logs new fills, order status changes and an equity row, and saves its state. `paper/report.py` builds the daily summary and re-runs a normal backtest on the market data the paper trader recorded, for the same period.

Things worth being able to point to in the code:

* where a taker order is prevented from trading at the price the strategy saw (`_execute_due_takers` runs only for orders with `active_from <= now` and `now > submitted_at`),
* where the queue estimate is set and updated (`FillModel.initial_queue_ahead`, `update_on_book`, `match_trade`),
* where position limits count open orders (`BacktestEngine._room`),
* why a late-arriving trade cannot fill an order that went live after it (`_on_trade` checks `activated_at <= event.timestamp`).

---

## Part 3: Interview questions

These are the kinds of questions a trader or researcher at Optiver, IMC, Jane Street, SIG or DRW might ask. The notes say what the question is really testing and what a good line of thought looks like. Work out your own answers; you will be more convincing in your own words.

1. **"Walk me through the project in two minutes."**
   Testing whether you can prioritise. Lead with the question you were trying to answer (can a simple market maker or arbitrage make money once you model fills, fees and latency honestly?), then the most interesting result (most of the naive profit was the queue assumption), then one limitation. Leave the code structure for follow-ups.

2. **"Your market maker loses money. Why should I be interested?"**
   Testing whether you understand that a negative result can be the valuable one. Talk about what the markouts and fill reasons show, and about having built a tool that tells you *why* a strategy loses. Then say what you would change and how you would test it.

3. **"How do you know your backtest doesn't have lookahead bias?"**
   Be specific: the strategy only gets a context built from past events; actions take effect after latency and strictly after the triggering event; taker orders execute on the next snapshot; the simulator's true probabilities never reach the strategy; there are tests for each. Then admit the remaining risks (e.g. trades and snapshots stamped by different clocks in real data).

4. **"When does your resting order get filled, and why did you choose those rules?"**
   The five fill rules, and for each one, whether it errs towards optimism or pessimism. A strong answer explains why filling on a book cross is the *conservative* choice even though it adds fills: those fills are adverse.

5. **"What is adverse selection, and how would you measure it?"**
   Define it with an example from this market, then markouts. Discuss the horizon: very short horizons measure spread capture, longer ones measure information. Mention that noise in the mid makes short-horizon markouts noisy.

6. **"Your skewed market maker makes less money than the unskewed one. Should you remove the skew?"**
   Testing risk thinking. Mean versus variance; the simulation has no drift, so inventory is free on average there; real markets may punish inventory more (inventory builds up when you are being picked off). Ask what the firm's objective is: expected P&L, Sharpe, or not blowing up.

7. **"How would you pick the half-spread?"**
   Think about the trade-off: wider quotes trade less but each trade earns more and is (maybe) less toxic. Then the process: parameter choice on one period, evaluation on a later one, and caution about how many settings you tried. Mention that the right width probably depends on volatility and the current spread, so a fixed number is a weak design.

8. **"What is the expected value of quoting a bid at 49 when the mid is 50?"**
   Make a small model: probability of being filled by an uninformed trader (you earn 1 cent of edge) versus an informed one (you lose the expected move), minus the fee. Show you can turn intuition into a calculation, and say which inputs you would estimate from data.

9. **"Kalshi's fee is 0.07 x P x (1 - P). Why that shape? How does it change your strategy?"**
   P(1 - P) is the variance of the payoff, so the fee scales with uncertainty. Fees are highest at 50 cents, so tight market making is hardest around 50 and easiest near the extremes, where the spread in cents is often similar but the fee is much smaller. For the arbitrage, three legs of taker fees are about 4 to 5 cents, more than most mispricings.

10. **"The outcome prices add up to 97. Free money?"**
    Check exhaustiveness (is there an "other" outcome?), fees on every leg, depth at those prices, latency and legging, how often others would race you for it, and the cost of capital locked until settlement. Then the result from this project: most such baskets did not fill completely.

11. **"Your arbitrage looks very profitable without fees and without an exposure cap. What's wrong with that number?"**
    Mark-to-market hides unhedged risk: the same positions at settlement have a huge standard deviation. Partly filled baskets are directional bets. This is a question about distrusting your own best number.

12. **"How would you make the arbitrage strategy better?"**
    Ideas: send the leg most likely to move first or last, use resting orders on some legs, only trade when depth covers the basket, repair or unwind partial baskets immediately and measure the cost of doing so, react to every book update rather than polls.

13. **"How confident are you in a result from 30 simulated sessions?"**
    Standard error of the mean, whether differences between scenarios are significant (paired comparison: the same 30 sessions are used for every scenario, which reduces noise), and the bigger point that the sessions all share the simulator's assumptions, so more seeds do not fix model error.

14. **"How did you build the simulator, and how would you know whether it is realistic?"**
    Explain the components, then calibration: compare distributions of spreads, trade sizes, trade rates, time between book changes, and markouts of real versus simulated trades. Mention the stylised facts you did not model (intraday patterns, volatility clustering, convergence to 0 or 100 near resolution).

15. **"What would change if you ran this live?"**
    Real queue position is unknown, other participants react to you, rate limits, connection failures, clock skew, partial fills, exchange rules on order types, capital and collateral. Mention the paper trader and what the paper-versus-backtest gap measures.

16. **"Why does a coarser polling interval make your market maker look worse?"**
    Stale quotes: between polls the strategy cannot update its prices, so moves run through them. A real market maker reacts to every update. Good follow-up: what would happen to an arbitrage strategy with coarser data (opportunities persist longer in the data but are less likely to be real).

17. **"How would you use the order book imbalance?"**
    As an input to fair value (microprice is one way), with a check that it actually predicts the next mid move out-of-sample, and care about how quickly the signal decays relative to your latency.

18. **"If you had to make this strategy profitable, what would you try first and why?"**
    Testing judgement about where the edge could come from: better fair value (less adverse selection), choosing which markets to quote (wider spreads, less informed flow, cheaper fees), and quoting less when it is dangerous (around news, when the book is thin). Explain how you would test each one without fooling yourself.

19. **"What's the difference between Sharpe within a session and across sessions here, and which do you trust?"**
    Within a session, 1-minute P&L changes are not independent (inventory carries over), so the ratio is distorted. Across sessions, each session is an independent sample. Discuss why annualising either is questionable.

20. **"What did you get wrong at first?"**
    Have an honest example ready. One from building this: the first version of the simulator redrew spreads and sizes independently every second, which made queue estimates collapse and flattered the market maker; making them persistent made the results worse and more realistic.

---

## Part 4: Improvements to build yourself

Ordered from easiest to hardest. Each one has a clear test of whether it worked. Doing these is what makes the project yours.

1. **Microprice or imbalance-adjusted fair value.** Switch the market maker to `fair_value="microprice"`, then try `mid + k x imbalance`. First check, on simulated and real data, whether imbalance predicts the mid change over the next few seconds (regress future mid change on imbalance). Success measure: less negative markouts, not just higher P&L.

2. **Volatility-aware quoting.** Make the half-spread depend on recent volatility of the mid (e.g. the standard deviation of mid changes over the last minute), and stop quoting for a few seconds after a large jump. Success measure: fewer book-cross fills and smaller drawdowns.

3. **Out-of-sample parameter selection.** Split sessions into a training set (e.g. seeds 0 to 19) and a test set (20 to 29), or real data into earlier and later days. Choose half-spread and skew on the training set only, report the test set. Then do walk-forward on real data. Success measure: an honest test-set number, and an estimate of how much performance drops out of sample.

4. **Adverse selection analysis.** Break markouts down by fill reason, by spread at the time of the fill, by trade size, and by time since the last price change. Find which fills lose money. Then change the strategy to avoid those situations, and check the improvement on held-out sessions.

5. **Basket repair for the arbitrage.** After a partial fill, decide between completing the basket (if the remaining legs still make the whole basket profitable after fees) and unwinding the filled legs. Measure the cost of each choice and the reduction in unhedged contracts and settlement variance.

6. **Bayesian updating of fair value.** Treat the fair probability as unknown with a prior (e.g. a Beta distribution or a Gaussian in logit space). Update it with each trade, weighting buys and sells by how informative you estimate them to be (informed fraction), and with the book. Quote around the posterior mean, with width tied to posterior uncertainty. This is a small version of the Glosten-Milgrom model. Success measure: markouts and P&L on held-out data compared with the mid-based baseline.

7. **Calibrate the simulator to real data.** From a week of collected data, estimate trade rates, size distributions, spread distribution and persistence, and how often the book moves through a level. Fit the simulator's parameters to match. Then check whether strategy results on the calibrated simulator are close to results on the real data. Success measure: the comparison table in `results/comparison_<name>/` shows similar numbers.

8. **A proper inventory model.** Implement Avellaneda-Stoikov properly: reservation price and optimal spread as functions of inventory, risk aversion, volatility and order arrival intensity, with intensity estimated from data. Compare against the linear skew on the mean-variance trade-off (plot mean P&L against P&L standard deviation for several risk aversions).
