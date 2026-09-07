# Feature Formulas Reference

This document contains the mathematical formulas for all features generated in the feature engineering pipeline. ll features are calculated over a rolling window of size $W$.

---

## Raw Inputs (Volatility Estimation Inputs)

These are the base inputs used for volatility feature calculations.

### r_1: Simple Returns
$$r_1 = \frac{C_t - C_{t-1}}{C_{t-1}}$$

### r_2: Log Returns
$$r_2 = \ln\left(\frac{C_t}{C_{t-1}}\right)$$

### r_3: Parkinson Input (High-Low Range)
$$r_3 = \ln\left(\frac{H_t}{L_t}\right)$$

### r_4: Rogers-Satchell Input
$$r_4 = \ln\left(\frac{H_t}{C_t}\right) \times \ln\left(\frac{H_t}{O_t}\right) + \ln\left(\frac{L_t}{C_t}\right) \times \ln\left(\frac{L_t}{O_t}\right)$$

### r_5: Overnight Returns (Open-to-Previous Close)
$$r_5 = \ln\left(\frac{O_t}{C_{t-1}}\right)$$

### r_6: Intraday Returns (Close-to-Open)
$$r_6 = \ln\left(\frac{C_t}{O_t}\right)$$


---

## 1. Volatility Features

### A. Simple and Log Returns Rolling Volatility
The standard deviation of returns over the lookback period.

**Simple Returns Volatility:**
$$\sigma_{simple} = \sqrt{\frac{1}{W-1} \sum_{i=1}^{W} (r_i - \bar{r})^2}, r_i = \frac{C_t - C_{t-1}}{C_{t-1}}$$

**Log Returns Volatility:**
$$\sigma_{log} = \sqrt{\frac{1}{W-1} \sum_{i=1}^{W} (ln\_r_i - \overline{ln\_r})^2}, \ln\_r_i = \ln\left(\frac{C_t}{C_{t-1}}\right)$$

### B. Parkinson’s Volatility
An estimator based on the High-Low range. It is significantly more efficient than standard deviation for capturing intra-period dynamics.
$$\sigma_{p} = \sqrt{\frac{1}{4W \ln(2)} \sum_{i=1}^{W} \left( \ln \frac{H_i}{L_i} \right)^2}$$

### B2. Garman-Klass Volatility
Uses the full OHLC bar; more efficient than Parkinson by adding the open-close term.
$$\sigma_{gk} = \sqrt{\frac{A}{W} \sum_{i=1}^{W} \left[ \tfrac{1}{2}\left(\ln \frac{H_i}{L_i}\right)^2 - (2\ln 2 - 1)\left(\ln \frac{C_i}{O_i}\right)^2 \right]}$$
where $A$ is the annualization factor (252 for daily bars).

### C. Rogers-Satchell Volatility
Captures volatility effectively in the presence of price trends (non-zero drift).
$$\sigma_{rs} = \sqrt{\frac{1}{W} \sum_{i=1}^{W} \left[ \ln \frac{H_i}{C_i} \ln \frac{H_i}{O_i} + \ln \frac{L_i}{C_i} \ln \frac{L_i}{O_i} \right]}$$

### D. Yang-Zhang Volatility
The minimum-variance estimator that combines overnight volatility and intra-day range.
$$\sigma_{yz}^2 = \sigma_{overnight}^2 + k \sigma_{oc}^2 + (1-k) \sigma_{rs}^2$$

**Where:**
* **Overnight Variance:** $\sigma_{overnight}^2 = \text{Var}(\ln \frac{O_i}{C_{i-1}})$
* **Open-to-Close Variance:** $\sigma_{oc}^2 = \text{Var}(\ln \frac{C_i}{O_i})$
* **Weighting Factor ($k$):** $k = \frac{0.34}{1.34 + \frac{W+1}{W-1}}$

### E. GARCH(1,1)
Models the conditional variance $\sigma_t^2$ based on previous squared residuals and variance.
$$\sigma_t^2 = \omega + \alpha \epsilon_{t-1}^2 + \beta \sigma_{t-1}^2$$

---

## 2. Trend Features

### A. Hurst Exponent ($H$)
Determines the persistence or mean-reverting nature of the time series.
$$E\left[ \frac{R(W)}{S(W)} \right] = C \cdot W^H$$
In practice, $H$ is the slope of the linear regression:
$$\ln\left(\frac{R}{S}\right) = H \ln(W) + \ln(C)$$

### B. Durbin-Watson Statistics ($DW$)
Detects first-order autocorrelation in the returns.
$$DW = \frac{\sum_{t=2}^{W} (r_t - r_{t-1})^2}{\sum_{t=1}^{W} r_t^2}$$

### C. Kaufman’s Efficiency Ratio ($ER$)
Quantifies the ratio between net price direction and total absolute price movement (noise).
$$ER = \frac{\left| C_t - C_{t-W} \right|}{\sum_{i=0}^{W-1} \left| C_{t-i} - C_{t-i-1} \right|}$$

---

## Notes

- All rolling features are computed with window sizes: **10, 20, 50, 100, 200**
- Formulas will be filled in by the user
