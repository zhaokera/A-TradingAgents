"""
自选股服务
"""

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from bson import ObjectId

from app.core.database import get_mongo_db
from app.models.user import FavoriteStock
from app.services.quotes_service import get_quotes_service


class FavoritesService:
    """自选股服务类"""
    
    def __init__(self):
        self.db = None
    
    async def _get_db(self):
        """获取数据库连接"""
        if self.db is None:
            self.db = get_mongo_db()
        return self.db

    def _is_valid_object_id(self, user_id: str) -> bool:
        """
        检查是否是有效的ObjectId格式
        注意：这里只检查格式，不代表数据库中实际存储的是ObjectId类型
        为了兼容性，我们统一使用 user_favorites 集合存储自选股
        """
        # 强制返回 False，统一使用 user_favorites 集合
        return False

    def _format_favorite(self, favorite: Dict[str, Any]) -> Dict[str, Any]:
        """格式化收藏条目（仅基础信息，不包含实时行情）。
        行情将在 get_user_favorites 中批量富集。
        """
        added_at = favorite.get("added_at")
        if isinstance(added_at, datetime):
            added_at = added_at.isoformat()
        ai_metadata = favorite.get("ai_metadata")
        ai_metadata = ai_metadata if isinstance(ai_metadata, dict) else None
        lifecycle_state = (
            str(ai_metadata.get("lifecycle_state") or "current")
            if favorite.get("source") == "ai_screening" and ai_metadata
            else "manual"
        )
        return {
            "stock_code": favorite.get("stock_code"),
            "stock_name": favorite.get("stock_name"),
            "market": favorite.get("market", "A股"),
            "added_at": added_at,
            "tags": favorite.get("tags", []),
            "notes": favorite.get("notes", ""),
            "alert_price_high": favorite.get("alert_price_high"),
            "alert_price_low": favorite.get("alert_price_low"),
            "source": favorite.get("source") or "manual",
            "ai_metadata": ai_metadata,
            "lifecycle_state": lifecycle_state,
            "is_current_ai_candidate": lifecycle_state == "current",
            # 行情占位，稍后填充
            "current_price": None,
            "change_percent": None,
            "volume": None,
            "quote_source": None,
            "quote_trade_at": None,
            "quote_checked_at": None,
        }

    async def get_user_favorites(self, user_id: str) -> List[Dict[str, Any]]:
        """获取用户自选股列表，并批量拉取实时行情进行富集（兼容字符串ID与ObjectId）。"""
        db = await self._get_db()

        favorites: List[Dict[str, Any]] = []
        if self._is_valid_object_id(user_id):
            # 先尝试使用 ObjectId 查询
            user = await db.users.find_one({"_id": ObjectId(user_id)})
            # 如果 ObjectId 查询失败，尝试使用字符串查询
            if user is None:
                user = await db.users.find_one({"_id": user_id})
            favorites = (user or {}).get("favorite_stocks", [])
        else:
            doc = await db.user_favorites.find_one({"user_id": user_id})
            favorites = (doc or {}).get("favorites", [])

        # 先格式化基础字段
        items = [self._format_favorite(fav) for fav in favorites]

        # 批量获取股票基础信息（板块等）
        codes = [it.get("stock_code") for it in items if it.get("stock_code")]
        if codes:
            try:
                # 🔥 获取数据源优先级配置
                from app.core.unified_config import UnifiedConfigManager
                config = UnifiedConfigManager()
                data_source_configs = await config.get_data_source_configs_async()

                # 提取启用的数据源，按优先级排序
                enabled_sources = [
                    ds.type.lower() for ds in data_source_configs
                    if ds.enabled and ds.type.lower() in ['tushare', 'akshare', 'baostock']
                ]

                if not enabled_sources:
                    enabled_sources = ['tushare', 'akshare', 'baostock']

                preferred_source = enabled_sources[0] if enabled_sources else 'tushare'

                # 从 stock_basic_info 获取板块信息（只查询优先级最高的数据源）
                basic_info_coll = db["stock_basic_info"]
                cursor = basic_info_coll.find(
                    {"code": {"$in": codes}, "source": preferred_source},  # 🔥 添加数据源筛选
                    {"code": 1, "sse": 1, "market": 1, "_id": 0}
                )
                basic_docs = await cursor.to_list(length=None)
                basic_map = {str(d.get("code")).zfill(6): d for d in (basic_docs or [])}

                for it in items:
                    code = it.get("stock_code")
                    basic = basic_map.get(code)
                    if basic:
                        # market 字段表示板块（主板、创业板、科创板等）
                        it["board"] = basic.get("market", "-")
                        # sse 字段表示交易所（上海证券交易所、深圳证券交易所等）
                        it["exchange"] = basic.get("sse", "-")
                    else:
                        it["board"] = "-"
                        it["exchange"] = "-"
            except Exception as e:
                # 查询失败时设置默认值
                for it in items:
                    it["board"] = "-"
                    it["exchange"] = "-"

        # 批量获取行情：腾讯直连优先，失败时由 QuotesService 统一降级。
        if codes:
            try:
                quotes_map = await get_quotes_service().get_quotes(codes)
                checked_at = datetime.utcnow().isoformat() + "Z"
                for it in items:
                    code = it.get("stock_code")
                    q = quotes_map.get(code)
                    if q:
                        it["current_price"] = (
                            q.get("price")
                            or q.get("close")
                            or q.get("current_price")
                        )
                        it["change_percent"] = q.get("pct_chg")
                        it["volume"] = q.get("volume")
                        it["quote_source"] = q.get("source") or q.get("data_source") or "fallback"
                        it["quote_trade_at"] = q.get("trade_at")
                        it["quote_checked_at"] = checked_at
            except Exception:
                # 查询失败时保持占位 None，避免影响基础功能
                pass

        return items

    async def add_favorite(
        self,
        user_id: str,
        stock_code: str,
        stock_name: str,
        market: str = "A股",
        tags: List[str] = None,
        notes: str = "",
        alert_price_high: Optional[float] = None,
        alert_price_low: Optional[float] = None,
        *,
        source: str = "manual",
        ai_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加股票到自选股（兼容字符串ID与ObjectId）"""
        import logging
        logger = logging.getLogger("webapi")

        try:
            logger.info(f"🔧 [add_favorite] 开始添加自选股: user_id={user_id}, stock_code={stock_code}")

            db = await self._get_db()
            logger.info(f"🔧 [add_favorite] 数据库连接获取成功")

            favorite_stock = {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "market": market,
                "added_at": datetime.utcnow(),
                "tags": tags or [],
                "notes": notes,
                "alert_price_high": alert_price_high,
                "alert_price_low": alert_price_low,
                "source": "ai_screening" if source == "ai_screening" else "manual",
                "ai_metadata": ai_metadata if source == "ai_screening" else None,
            }

            logger.info(f"🔧 [add_favorite] 自选股数据构建完成: {favorite_stock}")

            is_oid = self._is_valid_object_id(user_id)
            logger.info(f"🔧 [add_favorite] 用户ID类型检查: is_valid_object_id={is_oid}")

            if is_oid:
                logger.info(f"🔧 [add_favorite] 使用 ObjectId 方式添加到 users 集合")

                # 先尝试使用 ObjectId 查询
                result = await db.users.update_one(
                    {"_id": ObjectId(user_id)},
                    {
                        "$push": {"favorite_stocks": favorite_stock},
                        "$setOnInsert": {"favorite_stocks": []}
                    }
                )
                logger.info(f"🔧 [add_favorite] ObjectId查询结果: matched_count={result.matched_count}, modified_count={result.modified_count}")

                # 如果 ObjectId 查询失败，尝试使用字符串查询
                if result.matched_count == 0:
                    logger.info(f"🔧 [add_favorite] ObjectId查询失败，尝试使用字符串ID查询")
                    result = await db.users.update_one(
                        {"_id": user_id},
                        {
                            "$push": {"favorite_stocks": favorite_stock}
                        }
                    )
                    logger.info(f"🔧 [add_favorite] 字符串ID查询结果: matched_count={result.matched_count}, modified_count={result.modified_count}")

                success = result.matched_count > 0
                logger.info(f"🔧 [add_favorite] 返回结果: {success}")
                return success
            else:
                logger.info(f"🔧 [add_favorite] 使用字符串ID方式添加到 user_favorites 集合")
                result = await db.user_favorites.update_one(
                    {"user_id": user_id},
                    {
                        "$setOnInsert": {"user_id": user_id, "created_at": datetime.utcnow()},
                        "$push": {"favorites": favorite_stock},
                        "$set": {"updated_at": datetime.utcnow()}
                    },
                    upsert=True
                )
                logger.info(f"🔧 [add_favorite] 更新结果: matched_count={result.matched_count}, modified_count={result.modified_count}, upserted_id={result.upserted_id}")
                logger.info(f"🔧 [add_favorite] 返回结果: True")
                return True
        except Exception as e:
            logger.error(f"❌ [add_favorite] 添加自选股异常: {type(e).__name__}: {str(e)}", exc_info=True)
            raise

    async def get_favorite_codes(self, user_id: str) -> set[str]:
        """Return normalized favorite codes without quote enrichment."""
        db = await self._get_db()
        doc = await db.user_favorites.find_one(
            {"user_id": user_id},
            {"favorites.stock_code": 1, "_id": 0},
        )
        return {
            str(item.get("stock_code") or "").strip()
            for item in (doc or {}).get("favorites", [])
            if str(item.get("stock_code") or "").strip()
        }

    async def remove_favorite(self, user_id: str, stock_code: str) -> bool:
        """从自选股中移除股票（兼容字符串ID与ObjectId）"""
        db = await self._get_db()

        if self._is_valid_object_id(user_id):
            # 先尝试使用 ObjectId 查询
            result = await db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$pull": {"favorite_stocks": {"stock_code": stock_code}}}
            )
            # 如果 ObjectId 查询失败，尝试使用字符串查询
            if result.matched_count == 0:
                result = await db.users.update_one(
                    {"_id": user_id},
                    {"$pull": {"favorite_stocks": {"stock_code": stock_code}}}
                )
            return result.modified_count > 0
        else:
            result = await db.user_favorites.update_one(
                {"user_id": user_id},
                {
                    "$pull": {"favorites": {"stock_code": stock_code}},
                    "$set": {"updated_at": datetime.utcnow()}
                }
            )
            return result.modified_count > 0

    async def update_favorite(
        self,
        user_id: str,
        stock_code: str,
        tags: Optional[List[str]] = None,
        notes: Optional[str] = None,
        alert_price_high: Optional[float] = None,
        alert_price_low: Optional[float] = None
    ) -> bool:
        """更新自选股信息（兼容字符串ID与ObjectId）"""
        db = await self._get_db()

        # 统一构建更新字段（根据不同集合的字段路径设置前缀）
        is_oid = self._is_valid_object_id(user_id)
        prefix = "favorite_stocks.$." if is_oid else "favorites.$."
        update_fields: Dict[str, Any] = {}
        if tags is not None:
            update_fields[prefix + "tags"] = tags
        if notes is not None:
            update_fields[prefix + "notes"] = notes
        if alert_price_high is not None:
            update_fields[prefix + "alert_price_high"] = alert_price_high
        if alert_price_low is not None:
            update_fields[prefix + "alert_price_low"] = alert_price_low

        if not update_fields:
            return True

        if is_oid:
            result = await db.users.update_one(
                {
                    "_id": ObjectId(user_id),
                    "favorite_stocks.stock_code": stock_code
                },
                {"$set": update_fields}
            )
            return result.modified_count > 0
        result = await db.user_favorites.update_one(
            {
                "user_id": user_id,
                "favorites.stock_code": stock_code
            },
            {
                "$set": {
                    **update_fields,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        return result.modified_count > 0

    async def update_ai_candidate_tracking(
        self,
        user_id: str,
        stock_code: str,
        candidate: Dict[str, Any],
    ) -> bool:
        """Refresh the persisted AI plan without overwriting user notes or tags."""

        db = await self._get_db()
        find_one = getattr(db.user_favorites, "find_one", None)
        doc = (
            await find_one(
                {
                    "user_id": str(user_id),
                    "favorites.stock_code": str(stock_code),
                    "favorites.source": "ai_screening",
                },
                {"favorites": 1},
            )
            if callable(find_one)
            else None
        )
        existing_metadata: Dict[str, Any] = {}
        for favorite in (doc or {}).get("favorites", []):
            if (
                str(favorite.get("stock_code") or "") == str(stock_code)
                and favorite.get("source") == "ai_screening"
                and isinstance(favorite.get("ai_metadata"), dict)
            ):
                existing_metadata = dict(favorite["ai_metadata"])
                break
        actionability = str(candidate.get("actionability") or "")
        lifecycle_state = (
            actionability
            if actionability in {"expired", "invalidated", "target_reached"}
            else "current"
        )
        ai_metadata = {
            **existing_metadata,
            "run_id": candidate.get("run_id"),
            "generated_at": candidate.get("generated_at"),
            "reason_summary": candidate.get("reason_summary"),
            "reference_price": candidate.get("reference_price"),
            "price_plan": candidate.get("price_plan"),
            "objective_id": candidate.get("objective_id"),
            "objective_label": candidate.get("objective_label"),
            "objective_tier": candidate.get("objective_tier"),
            "objective_tier_label": candidate.get("objective_tier_label"),
            "objective_segment": candidate.get("objective_segment"),
            "source": candidate.get("source"),
            "tracking_enabled": True,
            "actionability": candidate.get("actionability"),
            "actionability_label": candidate.get("actionability_label"),
            "rank_score": candidate.get("rank_score"),
            "position_sizing": candidate.get("position_sizing"),
            "performance": candidate.get("performance"),
            "last_checked_at": candidate.get("quote_checked_at"),
            "quote_source": candidate.get("quote_source"),
            "quote_trade_at": candidate.get("trade_at"),
            "is_reference_only": True,
            "lifecycle_state": lifecycle_state,
            "is_current": lifecycle_state == "current",
            "superseded_at": None,
            "superseded_by_run_id": None,
        }
        result = await db.user_favorites.update_one(
            {
                "user_id": str(user_id),
                "favorites.stock_code": str(stock_code),
                "favorites.source": "ai_screening",
            },
            {
                "$set": {
                    "favorites.$.ai_metadata": ai_metadata,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        return result.modified_count > 0

    async def reconcile_ai_candidate_lifecycle(
        self,
        user_id: str,
        *,
        current_run_id: str,
        current_codes: List[str],
        generated_at: datetime,
    ) -> Dict[str, int]:
        """Archive prior AI picks while preserving every user-owned field."""

        db = await self._get_db()
        doc = await db.user_favorites.find_one({"user_id": str(user_id)})
        if not doc:
            return {"current": 0, "superseded": 0}
        current_code_set = {str(code) for code in current_codes}
        favorites = list(doc.get("favorites") or [])
        current_count = 0
        superseded_count = 0
        changed = False
        timestamp = generated_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_text = timestamp.isoformat()
        for favorite in favorites:
            if favorite.get("source") != "ai_screening":
                continue
            metadata = dict(favorite.get("ai_metadata") or {})
            code = str(favorite.get("stock_code") or "")
            if code in current_code_set:
                current_count += 1
                desired = {
                    "lifecycle_state": "current",
                    "is_current": True,
                    "run_id": current_run_id,
                    "superseded_at": None,
                    "superseded_by_run_id": None,
                }
            else:
                superseded_count += 1
                desired = {
                    "lifecycle_state": "superseded",
                    "is_current": False,
                    "superseded_at": timestamp_text,
                    "superseded_by_run_id": current_run_id,
                }
            if any(metadata.get(key) != value for key, value in desired.items()):
                metadata.update(desired)
                favorite["ai_metadata"] = metadata
                changed = True
        if changed:
            await db.user_favorites.update_one(
                {"user_id": str(user_id)},
                {
                    "$set": {
                        "favorites": favorites,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
        return {"current": current_count, "superseded": superseded_count}

    async def is_favorite(self, user_id: str, stock_code: str) -> bool:
        """检查股票是否在自选股中（兼容字符串ID与ObjectId）"""
        import logging
        logger = logging.getLogger("webapi")

        try:
            logger.info(f"🔧 [is_favorite] 检查自选股: user_id={user_id}, stock_code={stock_code}")

            db = await self._get_db()

            is_oid = self._is_valid_object_id(user_id)
            logger.info(f"🔧 [is_favorite] 用户ID类型: is_valid_object_id={is_oid}")

            if is_oid:
                # 先尝试使用 ObjectId 查询
                user = await db.users.find_one(
                    {
                        "_id": ObjectId(user_id),
                        "favorite_stocks.stock_code": stock_code
                    }
                )

                # 如果 ObjectId 查询失败，尝试使用字符串查询
                if user is None:
                    logger.info(f"🔧 [is_favorite] ObjectId查询未找到，尝试使用字符串ID查询")
                    user = await db.users.find_one(
                        {
                            "_id": user_id,
                            "favorite_stocks.stock_code": stock_code
                        }
                    )

                result = user is not None
                logger.info(f"🔧 [is_favorite] 查询结果: {result}")
                return result
            else:
                doc = await db.user_favorites.find_one(
                    {
                        "user_id": user_id,
                        "favorites.stock_code": stock_code
                    }
                )
                result = doc is not None
                logger.info(f"🔧 [is_favorite] 字符串ID查询结果: {result}")
                return result
        except Exception as e:
            logger.error(f"❌ [is_favorite] 检查自选股异常: {type(e).__name__}: {str(e)}", exc_info=True)
            raise

    async def get_user_tags(self, user_id: str) -> List[str]:
        """获取用户使用的所有标签（兼容字符串ID与ObjectId）"""
        db = await self._get_db()

        if self._is_valid_object_id(user_id):
            pipeline = [
                {"$match": {"_id": ObjectId(user_id)}},
                {"$unwind": "$favorite_stocks"},
                {"$unwind": "$favorite_stocks.tags"},
                {"$group": {"_id": "$favorite_stocks.tags"}},
                {"$sort": {"_id": 1}}
            ]
            result = await db.users.aggregate(pipeline).to_list(None)
        else:
            pipeline = [
                {"$match": {"user_id": user_id}},
                {"$unwind": "$favorites"},
                {"$unwind": "$favorites.tags"},
                {"$group": {"_id": "$favorites.tags"}},
                {"$sort": {"_id": 1}}
            ]
            result = await db.user_favorites.aggregate(pipeline).to_list(None)

        return [item["_id"] for item in result if item.get("_id")]

    def _get_mock_price(self, stock_code: str) -> float:
        """获取模拟股价"""
        # 基于股票代码生成模拟价格
        base_price = hash(stock_code) % 100 + 10
        return round(base_price + (hash(stock_code) % 1000) / 100, 2)
    
    def _get_mock_change(self, stock_code: str) -> float:
        """获取模拟涨跌幅"""
        # 基于股票代码生成模拟涨跌幅
        change = (hash(stock_code) % 2000 - 1000) / 100
        return round(change, 2)
    
    def _get_mock_volume(self, stock_code: str) -> int:
        """获取模拟成交量"""
        # 基于股票代码生成模拟成交量
        return (hash(stock_code) % 10000 + 1000) * 100


# 创建全局实例
favorites_service = FavoritesService()
