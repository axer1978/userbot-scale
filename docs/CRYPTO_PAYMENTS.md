# Crypto payments: choosing a processor

Checked on 2026-10-26 against each processor's current public docs, terms and coin lists. This is the "crypto" rail from section 2 of the client portal plan.

**Result: none of the four candidates meets all three needs.** As the brief asked, nothing has been built: no adapter, no tables, no routes. You need to choose one of the options in [What would unblock it](#what-would-unblock-it).

## The three needs

1. **Coins.** It accepts USDT, USDC, BTC and XMR. USDT and USDC must each have at least one low-fee network (for example USDT on TRON, USDC on Polygon or Solana), not only Ethereum mainnet.
2. **Terms.** Its terms don't prohibit adult services. The lines are quoted below.
3. **Payout.** It pays out in EUR or in a stablecoin, so we never hold volatile coins.

## Result at a glance

| Processor | 1. Coins and networks | 2. Terms allow adult services | 3. Pays out in EUR or stablecoin |
|---|---|---|---|
| **NOWPayments** | Yes: USDT on TRON, USDC on Polygon and Solana, BTC, XMR | **No.** Terms §14.1.5 names "sites offering services such as prostitution, escorts" as a forbidden business. §15.1 also excludes residents and citizens of the EU | Yes: automatic conversion to a coin of your choice; EUR through a third-party off-ramp |
| **CoinGate** | **No.** No USDT at all (dropped in 2025) and no XMR | **Unclear, treat as restricted.** Adult content is allowed only "with age verification". Its broad reputational-risk clause applies, and its terms in force until 2026 named prostitution | Yes: EUR by SEPA, or USDC / EURC |
| **Plisio** | Yes: USDT on TRON; USDC on Solana, Base or BSC (not Polygon); BTC; XMR | Yes. The terms have no business list, and Plisio's own blog courts "Adult dating and escort services" | **No.** No fiat payout, and no automatic conversion to a stablecoin; exchanging is manual and "may be frozen" |
| **BTCPay Server** (self-hosted) | **No.** BTC is native, XMR comes through a plugin, USDT through a pre-release plugin, but no plugin receives USDC | Yes. It is MIT-licensed software with no merchant terms | **No.** It is non-custodial: we would hold whatever arrives. Automatic conversion exists only for Lightning BTC, through third-party plugins |

How close each one is:

- **Plisio** fails only need 3.
- **NOWPayments** fails only need 2, but that failure is explicit, and §15.1 is a second blocker for an EU-based operator.
- **CoinGate** fails need 1 outright and probably need 2.
- **BTCPay Server** fails needs 1 and 3.

---

## NOWPayments

**1. Coins: yes.** The supported-coins page (https://nowpayments.io/supported-coins) lists these tickers with these names:

| Coin and network | Ticker | Name on the page |
|---|---|---|
| USDT on TRON | `usdttrc20` | "Tether (Tron)" |
| USDC on Polygon | `usdcmatic` | "USD Coin (Polygon)" |
| USDC on Solana | `usdcsol` | "USD Coin (Solana)" |
| BTC (on-chain only, no Lightning) | `btc` | "Bitcoin" |
| XMR | `xmr` | "Monero" |

Which of these an account may use comes from `GET /v1/merchant/coins`, which needs an API key, so it was not checked.

**2. Terms: no.** The terms are the "FD Transfers LLC Terms of Service, Last updated: OCTOBER 19, 2026", at https://nowpayments.io/doc/fd-tos.pdf?v=1.6. Verbatim:

> 14.1. You agree that You are responsible for your own conduct while accessing or using Services and for any consequences thereof. You agree to use Services only for purposes that are legal, proper and in accordance with these Terms and any applicable laws or regulations. By way of example, and not as a limitation, You may not allow any third party to: […]
>
> 14.1.5. carry on any unlawful businesses and activities such as, for example: (i) creation, sale or distribution of sites offering services such as prostitution, escorts; (ii) fraudulent business, sale of counterfeit or stolen items or the sale of goods or service that are illegally imported or exported; (iii) sale of narcotics, illegal substances or controlled substances, any equipment for making or using such drugs; (iv) pyramid schemes, high risk investments schemes and other business that we determine in our sole discretion to be unfair, deceptive or predatory towards consumers; (v) any business that we believe poses elevated financial risk, illegal liability, or violates the law of sender or recipient of payment;

> 15.1. The Services are not rendered to residents or citizens of the European Union, the United Kingdom, the United States of America, the United Arab Emirates, the Russian Federation, or any jurisdiction where the use of cryptocurrency services is restricted or prohibited by applicable law. […]

Our customers would be escort businesses paying for a tool that runs their bookings. Under §14.1.5 that is exactly the kind of account they may close at once: §14.2 lets them "block, terminate or deny access … without the Merchant's consent or notice". This is the Stripe problem from the plan again, so the same rule applies: don't put this niche on a rail whose terms forbid it.

**Their marketing says the opposite, but it is not the contract.** Two examples:

- https://nowpayments.io/blog/adult-industry-anonymous lists "Escort and dating services" among adult businesses and says "NOWPayments stands out as an ideal adult payment processor".
- https://nowpayments.io/all-solutions/adult offers "KYB-free crypto payments".

The signed terms are what counts in a dispute.

**3. Payout: yes.**

- Stablecoin, from the API docs (https://documenter.getpostman.com/view/7907941/2s93JusNJt): "Accept payments in a wide range of cryptos and get them instantly converted into a coin of your choice and sent to your Custody balance."
- EUR goes through a third party. ToS §10.5: "Fiat withdrawal is available exclusively for balances in Tether (USDT) on the TRON blockchain ("USDT TRC20")." It needs Custody turned on and the provider's own KYB check (§10.2, §10.8).

## CoinGate

**1. Coins: no.** The live list at https://api.coingate.com/v2/currencies (public, fetched 2026-10-26) has:

- USDC on Ethereum, BSC, Solana, Polygon, Arbitrum, Base and Optimism;
- BTC on Bitcoin and Lightning;
- **no USDT and no XMR.**

USDT was dropped for MiCA. From https://coingate.com/blog/post/usdt-dai-discontinued: "From February 26, 2025: … USDT stopped working as a settlement currency". No Monero listing or delisting notice was found; the coin list has had no XMR since at least January 2022, going by archived copies.

**2. Terms: unclear, treat as restricted.**

The General Terms and Conditions are at https://coingate.com/policy/general-terms-and-conditions (v1, published 2025-12-01, in force 2026-01-01). §12 is titled "PROHIBITED BUSINESSES" and opens: "We will not onboard You, or We may terminate immediately, if You engage in or are connected to any of the following:". It includes:

> Adult content without age verification or in breach of applicable laws and content rules.
>
> Any other activity that, in Our reasonable opinion, poses legal, compliance, reputational, or operational risk.

§11 adds "Engaging in activity that violates public order, good morals, …".

The Merchant Terms of Services were valid until 2026-01-01 (https://coingate.com/policy/merchant-terms-of-services). Clause 27.5.5 named "prostitution, management of brothels" among the grounds to refuse or terminate.

So escort services are not named in the current terms, but "connected to", "good morals" and "reputational risk" leave the decision to CoinGate. It would need their written approval first.

**3. Payout: yes.** From https://coingate.com/blog/post/crypto-payment-settlement-currency:

- "We settle fiat in USD, EUR and GBP only."
- "the menu is USDC and EURC, and nothing else."
- SEPA withdrawals are free, with a 50 EUR minimum (https://coingate.com/pricing).

## Plisio

**1. Coins: yes.** From https://plisio.net/documentation/appendices/supported-cryptocurrencies:

| Coin and network | Plisio ID |
|---|---|
| USDT on TRON | `USDT_TRX` ("Tether TRC-20") |
| USDC on Solana | `USDC_SOL` ("USDC spl") |
| USDC on Base | `USDC_BASE` |
| USDC on BSC | BEP-20 |
| BTC | `BTC` |
| XMR | `XMR` ("Monero") |

There is no USDC on Polygon. The brief allows Solana, so need 1 is met.

**2. Terms: yes.** The Terms of Use are at https://plisio.net/docs/plisio-terms-of-use.pdf ("Last Updated: March 12, 2019"). They have no list of prohibited businesses, and the words adult, escort, porn and dating do not appear. The only limits on use are:

> If you use our Services, you agree to do so in accordance with these Terms and applicable laws and regulations. You also represent and warrant that you will not use the Site if the laws of your country prohibit you from doing so in accordance with these Terms.

> … for accessing and using the Services solely for approved purposes as determined by Plisio. Any other use of the Services is strictly prohibited.

On top of that, the sanctions clauses. Plisio's own blog (https://plisio.net/blog/adult-crypto-payment-processing-path-to-secure-anonymous-transactions) says "Plisio specializes in providing adult merchant accounts tailored for this industry". It lists "Adult dating and escort services" among its examples.

**3. Payout: no.**

- No fiat. https://plisio.net/faq/where-i-can-buy-or-sell-cryptocurrency-for-money-dollars-euros-etc: "At the moment, Plisio service does not provide cryptocurrency exchange services for fiat money."
- No automatic conversion. Incoming BTC or XMR stays BTC or XMR. Converting is a manual step, either the dashboard's Exchange button or a separate exchange API call. From https://plisio.net/faq/cryptocurrency-exchange: "Every exchange transaction will be checked and may be frozen."
- The per-coin marketing pages are ambiguous. https://plisio.net/accept-bitcoin says "You receive the amount in the chosen currency (excluding a 0.5% fee)". Neither the API documentation nor the FAQ describes a setting that does this, so it does not count until Plisio confirms it in writing.

## BTCPay Server (self-hosted)

**1. Coins: no.**

- BTC is native, on-chain and Lightning.
- XMR comes through the community Monero plugin (https://github.com/btcpay-monero/btcpayserver-monero-plugin). It is maintained, but uses one Monero wallet for every store on the server.
- USDT on TRON (also Ethereum and Polygon) comes through the "Tether USDt" plugin (https://plugin-builder.btcpayserver.org/public/plugins/tether-usdt). Every recent release is marked pre-release, and it needs our own TRON node or a TronGrid key.
- **No plugin receives USDC.** The plugin directory (https://plugin-builder.btcpayserver.org/public/plugins) has only swap plugins, where the customer pays in USDC and the store receives BTC.

**2. Terms: yes.** It is MIT-licensed software. From https://docs.btcpayserver.org/FAQ/General/:

> BTCPay Server is an open-source self-hosted software stack, not a company.

> The community and contributors behind BTCPay Server have no control over who uses the software or how they use it.

Only the hosting provider's terms, and those of any third-party plugin service, would apply.

**3. Payout: no.** Payments go straight to our own wallets, so we would hold BTC and XMR. The FAQ says only:

> Fiat conversion is available through various BTCPay Server plugins.

The plugins that convert are third-party and handle Lightning BTC only: Bringin to EUR, SideShift or Flint to stablecoins. Nothing converts on-chain BTC or XMR.

---

## Confirmation times

Whichever processor is chosen, its own "paid" status is the one to use. None of the four publishes a per-coin confirmation table:

| Processor | What it says about confirmations | Source |
|---|---|---|
| NOWPayments | "each currency has its own amount of confirmations required to start the processing" | https://documenter.getpostman.com/view/7907941/2s93JusNJt |
| Plisio | Each invoice carries an `expected_confirmations` field | https://plisio.net/documentation/endpoints/create-an-invoice |
| CoinGate | It "depends on the asset and network" | https://coingate.com/glossary/confirmation |
| BTCPay Monero plugin | Chosen per store: the store's speed policy (0, 1, 2 or 6), zero, at least one, at least ten, or a custom number | https://github.com/btcpay-monero/btcpayserver-monero-plugin |

What to tell a customer, from block times on each network. Each confirmation is one block, so the wait is roughly the confirmations required times the block time.

| Coin on network | Block time | Typical wait for "paid" (estimate) |
|---|---|---|
| USDT on TRON | 3 s (https://developers.tron.network/docs/block) | about 1–5 minutes |
| USDC on Polygon | ~5 s per CoinGate (https://coingate.com/accept/usdc) | about 1–5 minutes |
| USDC on Solana | ~5 s per CoinGate (same page) | under a minute to a few minutes |
| BTC | 10 min on average (difficulty is set so 2,016 blocks take two weeks: https://developer.bitcoin.org/devguide/block_chain.html) | **10–60 minutes** (1–6 confirmations); longer when the network is busy |
| XMR | ~2 min (https://www.getmonero.org/get-started/faq/) | **about 20 minutes** at 10 confirmations |

**BTC and XMR are the slow ones.** The payment page should say so next to those two coins.

## What would unblock it

Pick one; each needs your decision, not code:

1. **Ask Plisio in writing** whether an invoice paid in BTC or XMR can settle automatically into USDT, as its coin pages suggest. If yes, Plisio meets all three needs and is the processor to build. This is one e-mail and the cheapest way forward.
2. **Relax need 3 and use Plisio as it is.**
   - Stablecoin payments arrive as stablecoins.
   - BTC and XMR payments are converted by us right after each payment, by hand or through Plisio's exchange API.
   - We hold BTC or XMR for minutes to hours, and an exchange "may be frozen" for checks.
3. **Relax need 1** and offer stablecoins only:
   - CoinGate: USDC only, no USDT. Its compliance team must also approve the business first.
   - Plisio: USDT and USDC, with BTC and XMR switched off.
4. **Look at processors outside this list.** None was checked here. Each would need the same three checks.

NOWPayments is not a realistic option while its terms name escort sites and exclude EU residents. Using it anyway would repeat the Stripe risk from the plan: an account closed without notice, with the balance in it.

## Not checked

- Coin lists tied to an account, such as NOWPayments' `GET /v1/merchant/coins` and Plisio's `GET /api/v1/currencies`. Both need an API key, and no account was created.
- Whether Plisio's "High-risk business" shop setting changes anything. It is listed at https://plisio.net/documentation/getting-started/setting-shop with no description.
- Any processor's answer to a direct question about this business. None was contacted.
