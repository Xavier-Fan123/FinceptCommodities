# Energy Commodities — A Field Guide to This Dashboard

A practical primer on the five energy contracts this dashboard tracks, and on
how to read each panel when you open one. Educational reference, not
investment advice. Open it any time from the footer link (`energy primer`).

---

## 1. The five contracts

| Dashboard id | Contract | Exchange | Size | Quoted | Tick (value) |
|---|---|---|---|---|---|
| `wti` | WTI Crude Oil (CL) | NYMEX | 1,000 bbl | USD/bbl | $0.01 ($10) |
| `brent` | Brent Crude (BZ, "Brent Last Day") | NYMEX (tracks ICE Brent) | 1,000 bbl | USD/bbl | $0.01 ($10) |
| `natgas` | Henry Hub Natural Gas (NG) | NYMEX | 10,000 MMBtu | USD/MMBtu | $0.001 ($10) |
| `rbob` | RBOB Gasoline (RB) | NYMEX | 42,000 gal | USD/gal | $0.0001 ($4.20) |
| `heating_oil` | NY Harbor ULSD (HO) | NYMEX | 42,000 gal | USD/gal | $0.0001 ($4.20) |

**WTI (West Texas Intermediate)** — the US benchmark. Light (~40° API) and
sweet (low sulfur), so it's cheap to refine into gasoline. The futures are
**physically delivered at Cushing, Oklahoma**, a landlocked tank farm at the
crossroads of the US pipeline network. That single fact explains a lot of WTI
behavior: when Cushing tanks fill up, the front contract can collapse — on
20 April 2020, the expiring May contract settled at **−$37.63/bbl** because
longs had nowhere to put the oil.

**Brent** — the waterborne global benchmark; roughly two-thirds of the
world's crude is priced off it. It's a basket of North Sea grades (plus WTI
Midland since 2023), slightly heavier and more sour than WTI. Being seaborne,
it reflects global supply/demand rather than US inland logistics. The `BZ`
contract here is NYMEX's financially-settled version of ICE Brent — same
price, cash-settled, no delivery.

**Henry Hub Natural Gas** — priced at the Henry Hub pipeline junction in
Erath, Louisiana. Gas is hard to store and expensive to move, so it is the
most weather-driven and most volatile contract on the board (annualized vol
regularly 50–80% vs ~30–40% for crude — compare *Risk* panels). Regional
"basis" hubs (e.g. Waha in West Texas) can trade far from Henry Hub, even
negative, when local pipelines are full.

**RBOB Gasoline** — "Reformulated Blendstock for Oxygenate Blending": the
petroleum part of gasoline *before* ~10% ethanol is splashed in at the
terminal. Delivered in New York Harbor. One contract = 1,000 bbl × 42 gal.

**ULSD (Heating Oil)** — ultra-low-sulfur diesel (≤15 ppm), NY Harbor. The
ticker HO survives from its heating-oil days; today it prices the diesel
complex: trucking, agriculture, industry, and winter heating.

**Quick mental math:** at $80 WTI a contract controls ~$80,000 of crude; at
$3.50 gas, ~$35,000. Products quote in $/gal — multiply by 42 to compare with
crude in $/bbl (e.g. $2.50/gal RBOB = $105/bbl).

---

## 2. Term structure — the *Futures Curve* panel

A futures curve plots prices across delivery months. Its shape is the
market's storage report:

- **Contango** (upward slope): ample supply. Carrying inventory is paid for
  by selling deferred months higher. The no-arbitrage ceiling is the
  **cost of carry**: `F ≈ S·e^((r + storage − convenience)·T)`.
- **Backwardation** (downward slope): tightness. Spot is bid because barrels
  are wanted *now*; the premium for holding physical is the
  **convenience yield** (the curve panel computes it as
  `c = r + u − ln(F/S)/T`).

Why it matters even if you never take delivery: **roll yield**. A long
futures position must periodically sell the expiring contract and buy the
next one. In backwardation the next contract is cheaper → rolling *earns*
(positive roll yield); in contango it *costs* (this is why oil ETFs bleed in
contango markets). The panel annualizes the front-roll and front→12m slopes
for you, and the header tag (contango / backwardation / mixed) classifies the
whole curve.

Energy specifics to look for:

- **WTI/Brent** flip between regimes with the cycle; persistent steep
  backwardation usually accompanies inventory draws.
- **Natural gas** has a *seasonal sawtooth* curve — winter months (Dec–Feb)
  price above summer months every year, so "contango vs backwardation" along
  the whole strip matters less than the **winter premium**. The Mar→Apr
  spread (end of withdrawal season vs start of injection) is so explosive
  it's nicknamed the **widow-maker**.
- **RBOB** carries a summer premium (driving season + costlier summer spec).

**The *Calendar Spreads* panel** turns the curve into the trader's working
view. The headline is the **prompt spread** (M1−M2) in the contract's own units:
positive means the front is *richer* than the next month — backwardation, the
signature of a market pulling barrels out of storage *now*. Below it the
**ladder** sets the front against the contracts nearest ~1, 2, 3, 6 and 12
months out, so you can see how fast the discount deepens with tenor and the
annualized roll each bucket implies. A backwardated, *steepening* prompt is the
cleanest curve-only read of tightness, and it tends to move *before* flat price.
(For natural gas the same panel exposes the seasonal sawtooth: the deferred
buckets straddle winter so the ladder bars flip sign along the strip, and the
Mar→Apr **widow-maker** sits in the adjacent-month spreads.)

---

## 3. Seasonality — the *Seasonality* and *Seasonal Price Bands* panels

Energy demand is a calendar phenomenon, and the panels make it visible:

- **Gasoline**: refiners switch to low-RVP **summer-grade** in spring
  (terminals by May, retail by June) — it's more expensive to make, right
  when **driving season** (Memorial Day → Labor Day) lifts demand. Spring
  also brings refinery maintenance ("turnarounds") that cuts supply. Net
  effect: RBOB strength typically builds Feb–May.
- **Natural gas**: **injection season** Apr–Oct (storage fills), **withdrawal
  season** Nov–Mar (storage drains). Price risk peaks around
  weather-forecast season changes (Oct–Jan). Shoulder months (Apr/May,
  Sep/Oct) are usually soft.
- **ULSD**: winter heating demand plus year-round freight; cold snaps in the
  US Northeast still move it.
- **Crude**: milder pattern — it inherits product seasonality through
  refinery demand (spring/fall turnarounds = less crude bought).

How to read the panels: the bar chart is the **average calendar-month
return** over up to 10 years (hover a bar for the win rate — the % of years
that month was positive — and sample size). The note beneath reports an
ANOVA test: treat "seasonality" as real only when it says statistically
detectable; 10 observations per month is a small sample. The **bands chart**
is the classic EIA-style view: the grey envelope is the historical range of
monthly average prices, the dashed line the average, the amber line the
current year — instantly shows whether this year is running hot or cold
versus history.

---

## 4. Refining economics — the *Spreads* tab

A refinery buys crude and sells products; its gross margin is the **crack
spread**. The dashboard computes the classic **3-2-1 crack**: 3 bbl of WTI in,
2 bbl of gasoline + 1 bbl of distillate out (≈ a typical US refinery's yield):

```
crack ($/bbl) = (2 × RBOB×42 + 1 × ULSD×42 − 3 × WTI) / 3
```

Reading it: wide cracks → refiners run flat-out (bullish crude demand,
eventually bearish products); narrow/negative cracks → run cuts (the
opposite). Historically ~$10–30/bbl; the 2022–2023 diesel squeeze pushed
cracks above $50. The tab shows the current level's 1-year percentile,
z-score, and a mean-reversion half-life — spreads are mean-reverting by
nature (they're a *margin*, arbitraged by physical players), which is why
those statistics are shown.

The tab runs the **whole refining suite** off both benchmarks: the **WTI** and
**Brent 3-2-1** cracks (the Brent crack is closer to the margin a coastal or
European refiner actually sees), plus the two single-cut cracks —
**gasoline (RBOB−WTI)** and **distillate (ULSD−WTI)**, each `product×42 − crude`
in $/bbl. The single cracks decompose the 3-2-1 and show *which* product is
pulling the barrel: in summer the gasoline crack usually leads; a firm
distillate crack flags freight, industrial or winter-heating strength. A wide
crack of any flavour pays refiners to buy more crude — bullish crude demand.

Also on the tab:

- **WTI − Brent**: location & quality. Pre-2010 WTI traded at a small
  premium; the shale boom flooded Cushing and the spread blew out to −$20+
  until the US **crude-export ban was lifted (Dec 2015)**. Since then it
  normally sits around −$2 to −$6 — roughly the cost of moving a barrel from
  Cushing to tidewater. A collapsing spread shuts US exports; a blowout
  signals inland glut.
- **Gold/Silver and Soybean Crush** — same mean-reversion toolkit applied to
  the metals ratio and the soybean processing margin (beans → meal + oil).

One classic energy spread is *not* here: the **spark spread**
(power − gas × heat rate, the gas generator's margin; the analytics module
supports it with a 7 MMBtu/MWh default heat rate ≈ 49%-efficient CCGT) —
there's no free, keyless power-price feed to drive it.

---

## 5. Positioning — the *COT Positioning* panel

Every Friday 3:30 PM ET the CFTC publishes the **Commitments of Traders**
report (data as of Tuesday). The *disaggregated* format splits open interest
into:

- **Producer/Merchant/Processor/User** — physical players hedging
  ("commercials"): producers selling forward, refiners/airlines buying.
- **Swap Dealers** — banks intermediating OTC hedges and index flows.
- **Managed Money** — hedge funds and CTAs: the *speculators* whose flows
  drive short-term trends.
- **Other Reportables / Non-reportables** — everyone else.

The panel shows managed-money **net** position (longs − shorts), its 1-week
change, commercials' net (almost always the mirror image — for every spec
long there's a hedger short), open interest, and a 52-week history.

The **COT index** rescales net positioning to 0–100 over the trailing 3
years. Readings ≥90 or ≤10 are flagged as extremes: a market where *everyone*
is already long has fewer marginal buyers left — crowded positioning is a
contrarian *condition* (fuel for a reversal), not a *timing signal*. The
divergence note (specs adding while hedgers add the other side, or specs
bailing while hedgers cover) gives the flow context. In crude, managed money
is structurally net long; in gas it swings hard with weather models.

---

## 6. Inventories — the weekly data calendar

Inventory statistics are the heartbeat of energy trading; most weekly
volatility clusters around them (all times US Eastern):

| When | Release | What moves |
|---|---|---|
| Tue 4:30 PM | API petroleum stocks (industry survey) | overnight crude |
| **Wed 10:30 AM** | **EIA Weekly Petroleum Status Report** | crude & products — stocks (incl. Cushing), refinery utilization, production, exports |
| **Thu 10:30 AM** | **EIA Weekly Natural Gas Storage** | natgas — injection/withdrawal in Bcf vs the 5-year range |
| Fri 1:00 PM | Baker Hughes rig count | crude/gas, slow-moving supply signal |

The market trades the **surprise vs expectations**, framed against the
**5-year seasonal range** — and that is exactly what the
**Inventories · EIA Weekly** panel shows on the wti, natgas, rbob, and
heating_oil pages: current stocks (crude ex-SPR in MMbbl, Lower-48 working
gas in Bcf) plotted inside the 5-year weekly band, the latest build/draw and
its streak, and where the level sits vs the 5-year average. Reading it:
stocks **below** the band with a draw streak = tight market (price-supportive,
shown green); **above** the band with builds = glut (red). Gas traders watch
whether storage will reach ~3,700+ Bcf by November (comfortable winter) —
follow the amber line's trajectory against the band through injection season.

**The *Crude Balance* panel (wti page)** widens that single crude-stocks number
into the whole US balance, all from the same Wednesday EIA report:

- **Cushing stocks** — the WTI delivery hub, with its own 5-year weekly band.
  This is the tank farm that prices the front contract: a low or falling
  Cushing (below the band) is what *makes* the prompt spread backwardate.
  Cross-check it against the *Calendar Spreads* panel — they should agree.
- **SPR** — the Strategic Petroleum Reserve, still well below its pre-2022 level.
- **Supply**: US field **production** and **imports**.
- **Refinery demand**: crude **runs** and **utilization %** — how hard refiners
  are pulling crude (seasonally peaking in summer driving season).
- **Exports** — the valve that drains US crude to the world; it widens when
  **WTI−Brent** is negative enough to pay the freight (tie it back to *Spreads*).

Each cell is coloured by its *crude-price* read, not its raw direction: a draw
in Cushing, or a rise in exports/runs, is green (tightening); a production or
import build is red. The identity underneath:
`Δstocks ≈ production + imports − refinery runs − exports`.

Monthly big-picture reports: EIA STEO, OPEC MOMR, IEA Oil Market Report.

---

## 7. Risk character — the *Risk* panel

What the numbers typically look like, and why:

- **Volatility ranking**: natgas ≫ products ≳ crude ≫ gold. Gas is
  storage-constrained and weather-driven; crude has OPEC and a global
  arbitrage network damping it.
- **Fat tails**: energy return distributions have high excess kurtosis —
  check the skew stat and compare VaR (95th percentile loss) with **CVaR**
  (the *average* loss beyond VaR). CVaR ≫ VaR = tail risk concentrated in
  jumps (war headlines, OPEC surprises, hurricanes, freeze-offs).
- **Drawdowns**: the drawdown sparkline shows how losses cluster; energy
  drawdowns tend to be fast (event-driven) rather than grinding.
- **Negative prices are possible** when storage/transport saturate: WTI April
  2020; Waha hub gas trades negative routinely during pipeline congestion.
  Percent-return math breaks down near zero — another reason gas risk stats
  look wild.
- **Leverage**: futures post ~5–10% margin, so a 3% daily move (an ordinary
  day in gas) is a 30–60% swing on margin capital.

---

## 8. Suggested tour

1. Open **natgas** → *Seasonal Price Bands*: the winter/summer sawtooth and
   where this year sits in the 10-year envelope.
2. Same page, *Futures Curve*: find the winter premium in the strip; check
   the roll-yield stat.
3. Open **wti** → *Calendar Spreads*: is the prompt (M1−M2) backwardated and
   does the ladder steepen out? Then *Crude Balance*: is Cushing below its
   5-year band (confirming it), and are runs/exports draining stock? Then
   *COT Positioning*: where is managed money vs its 3-year range?
4. **Spreads tab**: is the 3-2-1 crack above or below its 1-year mean, and is
   the gasoline or distillate crack leading? What does that imply for refinery
   runs — and therefore crude demand?
5. Open **rbob** → *Seasonality*: the spring ramp into driving season, and
   the win rates behind it.
6. Back on **natgas** → *Inventories*: is the amber storage line tracking
   above or below the 5-year band, and is the gap widening or closing as
   injection season progresses? Cross-check what the futures curve's winter
   premium says about it.

---

## Glossary

| Term | Meaning |
|---|---|
| bbl | barrel = 42 US gallons |
| MMBtu | million British thermal units (≈ 1,000 cu ft of gas) |
| API gravity | crude lightness scale; higher = lighter = more gasoline yield |
| sweet / sour | low / high sulfur crude |
| prompt / front month | nearest-delivery futures contract |
| strip | average of several consecutive contract months (e.g. winter strip = Nov–Mar) |
| basis | price difference between a regional hub/grade and the benchmark |
| contango / backwardation | upward / downward sloping futures curve |
| roll yield | P&L from replacing an expiring contract with the next one |
| convenience yield | implied benefit of holding physical inventory now |
| crack / crush / spark | processing margins: refinery / soybean / gas-fired power |
| RVP | Reid Vapor Pressure — summer gasoline spec (lower evaporation) |
| turnaround | scheduled refinery maintenance (spring/fall) |
| COT | CFTC Commitments of Traders positioning report |
| open interest | total outstanding contracts |
| widow-maker | the NG March/April calendar spread |
