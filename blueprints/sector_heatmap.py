"""Sector Heatmap Blueprint

Serves the NSE sectoral-index universe for the sector heatmap.

Endpoints:
    GET /sectorheatmap/api/universe
        Returns each sector with its constituents and the de-duplicated symbol
        list to subscribe to over the market-data WebSocket.
"""

from flask import Blueprint, jsonify, session
from flask_cors import cross_origin

from services.sector_heatmap_service import get_sector_universe
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

sector_heatmap_bp = Blueprint("sector_heatmap_bp", __name__, url_prefix="/")


@sector_heatmap_bp.route("/sectorheatmap/api/universe", methods=["GET"])
@cross_origin()
@check_session_validity
def sector_heatmap_universe():
    """Return the sector universe."""
    try:
        if not session.get("user"):
            return jsonify({"status": "error", "message": "Authentication required"}), 401
        _, response, status_code = get_sector_universe()
        return jsonify(response), status_code
    except Exception as e:
        logger.exception(f"Error in sector heatmap universe API: {e}")
        return (
            jsonify({"status": "error", "message": "An error occurred processing your request"}),
            500,
        )
