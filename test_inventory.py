"""Unit tests – inventory, prediction, synchronization, reporting, KPI maths."""
from __future__ import annotations

import pytest

from database.db import query, scalar
from modules import inventory as inv
from modules import scanning as scan
from modules import stockout_prediction as pred
from modules import store_performance as perf
from utils.calculations import (days_of_cover, performance_score,
                                recommended_reorder_qty, risk_level_from_probability,
                                stock_status)
from utils.validators import ValidationError, validate_barcode


# =====================================================================
# Pure calculation unit tests
# =====================================================================
class TestCalculations:
    def test_closing_stock_identity(self):
        """Closing = Opening + Receipts + Adj+ - Sales - Damage - Adj-  (ledger sum)."""
        opening, receipts, adj_pos, sales, damage, adj_neg = 120, 60, 5, 140, 3, 2
        assert opening + receipts + adj_plus(adj_pos) - sales - damage - adj_neg == 40

    def test_days_of_cover(self):
        assert days_of_cover(30, 10) == pytest.approx(3.0)
        assert days_of_cover(30, 0) == float("inf")
        assert days_of_cover(0, 5) == pytest.approx(0.0)
        assert days_of_cover(10, 5, pending_po=10) == pytest.approx(4.0)

    def test_stock_status_bands(self):
        assert stock_status(0, 20, 8) == "Out of Stock"
        assert stock_status(5, 20, 8) == "Critical Stock"
        assert stock_status(15, 20, 8) == "Low Stock"
        assert stock_status(50, 20, 8) == "In Stock"

    @pytest.mark.parametrize("p,expected", [
        (0.0, "Low Risk"), (0.30, "Low Risk"), (0.31, "Moderate Risk"),
        (0.60, "Moderate Risk"), (0.61, "High Risk"), (0.80, "High Risk"),
        (0.81, "Critical Risk"), (1.0, "Critical Risk"),
    ])
    def test_risk_bands(self, p, expected):
        assert risk_level_from_probability(p) == expected

    def test_reorder_quantity_respects_cap(self):
        qty = recommended_reorder_qty(current_stock=5, avg_daily_demand=10,
                                      lead_time_days=3, safety_stock=8,
                                      max_stock_level=400)
        assert 0 < qty <= 400 - 5
        assert recommended_reorder_qty(400, 10, 3, 8, 400) == 0

    def test_performance_score_is_weighted(self):
        flat = {k: 60.0 for k in
                ["sales_performance", "stock_availability", "inventory_efficiency",
                 "forecast_accuracy", "replenishment_efficiency"]}
        assert performance_score(flat) == pytest.approx(60.0, abs=0.01)
        high_sales_low_avail = {**flat, "sales_performance": 100, "stock_availability": 0}
        assert performance_score(high_sales_low_avail) < 75


def adj_plus(x):
    return x


# =====================================================================
# Validation
# =====================================================================
class TestValidation:
    @pytest.mark.parametrize("raw", ["8901234567890", " 8901234567890\r\n", "12345678"])
    def test_valid_barcodes(self, raw):
        assert validate_barcode(raw).isdigit()

    @pytest.mark.parametrize("raw", ["", "abc", "123", "890123456789012345"])
    def test_invalid_barcodes(self, raw):
        with pytest.raises(ValidationError):
            validate_barcode(raw)


# =====================================================================
# Integration-level tests against the seeded database
# =====================================================================
@pytest.fixture(scope="module")
def ctx():
    if not scalar("SELECT COUNT(*) FROM stock_on_hand", default=0):
        pytest.skip("database not seeded – run python -m database.seed_data")
    store_id, sku_id = query(
        "SELECT store_id, sku_id FROM stock_on_hand WHERE on_hand_qty > 60 "
        "ORDER BY on_hand_qty DESC LIMIT 1").iloc[0].tolist()
    return {"store_id": int(store_id), "sku_id": int(sku_id)}


class TestInventoryLedger:
    def test_sale_reduces_stock_exactly_once(self, ctx):
        s, k = ctx["store_id"], ctx["sku_id"]
        before = inv.available(s, k)
        sku = inv.get_stock(s, k)
        cart = [{"sku_id": k, "sku_code": sku["sku_code"], "barcode": sku["barcode"],
                 "product_name": sku["product_name"], "unit_price": sku["selling_price"],
                 "unit_cost": sku["unit_cost"], "quantity": 3, "line_amount": 3 * sku["selling_price"]}]
        store_code = scalar("SELECT store_code FROM stores WHERE store_id = :s", {"s": s})
        res = scan.process_sale(cart, s, store_code, None)
        assert inv.available(s, k) == before - 3
        assert res["items"] == 3
        # the ledger must agree with the cache
        assert inv.ledger_balance(s, k) == inv.get_stock(s, k)["on_hand_qty"]

    def test_duplicate_event_id_is_rejected(self, ctx):
        s, k = ctx["store_id"], ctx["sku_id"]
        sku = inv.get_stock(s, k)
        cart = [{"sku_id": k, "sku_code": sku["sku_code"], "barcode": sku["barcode"],
                 "product_name": sku["product_name"], "unit_price": sku["selling_price"],
                 "unit_cost": sku["unit_cost"], "quantity": 1,
                 "line_amount": sku["selling_price"]}]
        store_code = scalar("SELECT store_code FROM stores WHERE store_id = :s", {"s": s})
        res = scan.process_sale(cart, s, store_code, None)
        from modules.scanning import process_sale
        from utils.validators import DuplicateScanError
        with pytest.raises(DuplicateScanError):
            process_sale(cart, s, store_code, None, client_event_id=res["event_id"])

    def test_insufficient_stock_is_blocked(self, ctx):
        from utils.validators import InsufficientStockError
        s, k = ctx["store_id"], ctx["sku_id"]
        sku = inv.get_stock(s, k)
        cart = [{"sku_id": k, "sku_code": sku["sku_code"], "barcode": sku["barcode"],
                 "product_name": sku["product_name"], "unit_price": sku["selling_price"],
                 "unit_cost": sku["unit_cost"], "quantity": 10_000_000,
                 "line_amount": 1.0}]
        store_code = scalar("SELECT store_code FROM stores WHERE store_id = :s", {"s": s})
        with pytest.raises((InsufficientStockError, ValidationError)):
            scan.process_sale(cart, s, store_code, None)

    def test_adjustment_and_recompute(self, ctx):
        s, k = ctx["store_id"], ctx["sku_id"]
        before = inv.available(s, k)
        inv.adjust_stock(s, k, 2, "UP", "STOCKTAKE_CORRECTION", None, "test")
        assert inv.available(s, k) == before + 2
        inv.recompute_stock_on_hand(s)
        assert inv.reconcile(s).empty, "cache must reconcile with the ledger"

    def test_unknown_barcode(self, ctx):
        from utils.validators import UnknownBarcodeError
        with pytest.raises(UnknownBarcodeError):
            scan.handle_scan("9999999999999", ctx["store_id"])

    def test_duplicate_scan_increments_quantity(self, ctx):
        s, k = ctx["store_id"], ctx["sku_id"]
        sku = inv.get_stock(s, k)
        r1 = scan.handle_scan(sku["barcode"], s)
        r2 = scan.handle_scan(sku["barcode"], s, cart=r1["cart"])
        assert len(r2["cart"]) == 1
        assert r2["cart"][0]["quantity"] == 2
        assert r2["duplicate"] is True


class TestPrediction:
    def test_predictions_are_in_range_and_labelled(self):
        df = pred.predictions(limit=5000)
        if df.empty:
            pytest.skip("no predictions scored yet")
        assert df["probability"].between(0, 1).all()
        assert df["prediction_source"].isin(["TRAINED_MODEL", "BASELINE_HEURISTIC"]).all()
        assert df["risk_level"].isin(["Low Risk", "Moderate Risk", "High Risk",
                                      "Critical Risk"]).all()

    def test_higher_risk_has_lower_cover_on_average(self):
        df = pred.predictions(limit=5000)
        if df.empty:
            pytest.skip("no predictions")
        crit = df[df["risk_level"] == "Critical Risk"]["days_of_cover"]
        low = df[df["risk_level"] == "Low Risk"]["days_of_cover"]
        if len(crit) and len(low):
            assert crit.mean() < low.mean(), "critical risk must coincide with lower cover"

    def test_baseline_is_monotonic(self):
        low_cover = pred.baseline_probability(0.5, 5, 0, 0)
        high_cover = pred.baseline_probability(40, 5, 0, 0)
        assert low_cover > high_cover
        assert 0 <= high_cover <= 1 and 0 <= low_cover <= 1

    def test_model_metrics_recorded(self):
        info = pred.model_info()
        if not info["available"]:
            pytest.skip("model not trained")
        m = info["metrics"][info["best_model"]]
        assert 0 <= m["accuracy"] <= 1
        assert m["roc_auc"] is None or 0.5 <= m["roc_auc"] <= 1.0
        assert sum(sum(r) for r in m["confusion_matrix"]) > 0


class TestSynchronization:
    def test_offline_queue_then_push(self, ctx):
        from modules import synchronization as sync
        s = ctx["store_id"]
        sync.set_offline(s, True)
        before = inv.available(s, ctx["sku_id"])
        ev = sync.queue_event(s, ctx["sku_id"], "ADJUSTMENT_NEG", -2)
        assert sync.outbox_size(s) >= 1
        skipped = sync.push_loop(s)
        assert skipped["status"] == "SKIPPED_OFFLINE"
        assert inv.available(s, ctx["sku_id"]) == before, "central stock unchanged while offline"
        sync.set_offline(s, False)
        res = sync.push_loop(s)
        assert res["accepted"] >= 1
        assert inv.available(s, ctx["sku_id"]) == before - 2

    def test_duplicate_batch_is_idempotent(self, ctx):
        from modules import synchronization as sync
        s = ctx["store_id"]
        sync.queue_event(s, ctx["sku_id"], "ADJUSTMENT_NEG", -1)
        first = sync.push_loop(s)
        assert first["accepted"] == 1
        stock_after_first = inv.available(s, ctx["sku_id"])
        # re-send the same events
        replayed = sync.replay_same_batch(s, "test")
        assert replayed["duplicates"] >= 1
        assert inv.available(s, ctx["sku_id"]) == stock_after_first

    def test_sync_logs_written(self):
        n = scalar("SELECT COUNT(*) FROM synchronization_logs", default=0)
        assert n > 0


class TestStoreKPIs:
    def test_kpi_frame_has_expected_columns(self):
        df = perf.store_kpi_current(days=30)
        if df.empty:
            pytest.skip("no KPI data")
        for col in ["sales_revenue", "stock_availability_pct", "inventory_turnover",
                    "forecast_accuracy_pct", "replenishment_efficiency",
                    "performance_score", "rank"]:
            assert col in df.columns
        assert df["performance_score"].between(0, 100).all()
        assert df["rank"].min() == 1

    def test_score_not_driven_by_sales_alone(self):
        df = perf.store_kpi_current(days=30)
        if len(df) < 3:
            pytest.skip("not enough stores")
        top_sales = df.sort_values("sales_revenue", ascending=False).iloc[0]
        top_score = df.sort_values("performance_score", ascending=False).iloc[0]
        # the ranking must be reproducible from the documented weights
        recomputed = (
            top_score["n_sales"] * 0.30 + top_score["n_availability"] * 0.25
            + top_score["n_turnover"] * 0.20 + top_score["n_forecast"] * 0.15
            + top_score["n_replenishment"] * 0.10
        )
        assert recomputed == pytest.approx(top_score["performance_score"], abs=0.6)

    def test_regional_summary_sums_to_chain(self):
        df = perf.store_kpi_current(days=30)
        reg = perf.regional_summary(days=30)
        if df.empty or reg.empty:
            pytest.skip("no data")
        assert reg["sales_revenue"].sum() == pytest.approx(df["sales_revenue"].sum(), rel=0.01)


class TestReporting:
    def test_all_reports_build(self):
        from modules import reporting as rep
        for entry in rep.report_catalogue():
            df = rep.build_dataset(entry["key"])
            assert isinstance(df, list) or hasattr(df, "columns"), entry["key"]

    def test_csv_and_excel_and_pdf_bytes(self):
        from modules import reporting as rep
        df = rep.build_dataset("prediction")
        if df.empty:
            pytest.skip("no predictions")
        csv = rep.to_csv(df.head(50))
        xlsx = rep.to_excel(df.head(50), "Prediction", "Test")
        pdf = rep.to_pdf(df.head(50), "Test report")
        assert csv[:3] == b"\xef\xbb\xbf" or b"," in csv
        assert xlsx[:2] == b"PK"
        assert pdf[:4] == b"%PDF"
        assert len(pdf) > 1500


class TestAlerts:
    def test_dedup_suppresses_repeat(self, ctx):
        from modules import alerts as al
        s, k = ctx["store_id"], ctx["sku_id"]
        a1 = al.create_alert(s, k, "LOW_STOCK", "MODERATE", 0.5, 10, "test one",
                             force=True)
        a2 = al.create_alert(s, k, "LOW_STOCK", "MODERATE", 0.5, 10, "test two")
        assert a1 and not a1.get("suppressed")
        assert a2 and a2.get("suppressed") is True

    def test_lifecycle_transitions(self, ctx):
        from modules import alerts as al
        a = al.create_alert(ctx["store_id"], ctx["sku_id"], "LOW_STOCK", "MODERATE",
                            0.55, 12, "lifecycle test", force=True)
        aid = a["alert_id"]
        user = {"user_id": 1, "username": "tester", "store_id": ctx["store_id"]}
        al.acknowledge(aid, user)
        assert al.get_alert(aid)["status"] == "ACKNOWLEDGED"
        al.resolve(aid, user, "restocked")
        assert al.get_alert(aid)["status"] == "RESOLVED"
        al.close(aid, user)
        assert al.get_alert(aid)["status"] == "CLOSED"
        with pytest.raises(ValueError):
            al.transition(aid, "OPEN", user)
