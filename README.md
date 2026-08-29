# 🚨 Razorpay AI Revenue Recovery

**Track 3 — AI Revenue Recovery | Razorpay AI Buildathon 2026**

---

## 🎯 Project Goal

We are building an **incident-aware revenue recovery system** for payment failures.

Instead of treating every failed payment individually, our system detects **groups of related failures** — called **payment incidents** — and uses an **Incident Recovery Twin** to compare and execute the best recovery strategy automatically.

## 📍 Current Development Stage

**Phase 1 — Foundation & Incident Detection**

We have built the project skeleton and a working Incident Detector that:

- Reads payment records from a CSV file
- Groups failures by **bank**, **payment method**, and **failure reason**
- Calculates failure rates per group
- Flags groups with abnormally high failure rates as potential **incidents**
- Prints clear, readable output

### What's NOT built yet

- ❌ No machine learning or anomaly detection
- ❌ No LLM / AI integration
- ❌ No Razorpay API connection
- ❌ No Incident Recovery Twin
- ❌ No frontend / web dashboard

## 🚀 How to Run

```bash
# Run the Incident Detector
python backend/incident_detector.py

# Run the tests
python -m pytest tests/ -v
```

## 📁 Project Structure

```
razorpay_ai_revenue_recovery/
├── backend/
│   └── incident_detector.py    # Core detection logic
├── data/
│   └── sample_payments.csv     # 35 sample payment records
├── tests/
│   └── test_incident_detector.py  # Unit + integration tests
├── README.md
├── PROJECT_DECISIONS.md
├── requirements.txt
└── .gitignore
```

## 🧠 How the Incident Detector Works (Simple Explanation)

1. **Read**: Load all payment records from a CSV file.
2. **Group**: Cluster payments by (bank + payment method + error reason).
3. **Count**: For each group, count total payments, failed payments, and revenue at risk.
4. **Flag**: If a group has ≥3 failures and a failure rate ≥50%, it's flagged as an **incident**.
5. **Report**: Print a clear summary with bank, method, reason, failure rate, and revenue at risk.

## 📄 License

Internal project — Razorpay AI Buildathon 2026.
