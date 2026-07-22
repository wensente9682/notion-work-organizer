from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
import json
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from total import TotalError, TotalItem, TotalResult, total


class TotalViewError(TotalError):
    pass


class NotionViewsApi:
    def __init__(self, token: str, *, opener: Any = None) -> None:
        self._token = token
        self._opener = opener or urllib.request.urlopen

    def _response(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
            raise TotalViewError("view query request failed") from exc
        if not isinstance(payload, dict):
            raise TotalViewError("view query response contract is incomplete")
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": "2026-03-11",
            "Content-Type": "application/json",
        }

    def create_view_query(self, view_id: str, *, page_size: int) -> dict[str, Any]:
        encoded_view = urllib.parse.quote(view_id, safe="")
        request = urllib.request.Request(
            f"https://api.notion.com/v1/views/{encoded_view}/queries",
            data=json.dumps({"page_size": page_size}).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        return self._response(request)

    def get_view_query_results(
        self,
        view_id: str,
        query_id: str,
        cursor: str,
        *,
        page_size: int,
    ) -> dict[str, Any]:
        encoded_view = urllib.parse.quote(view_id, safe="")
        encoded_query = urllib.parse.quote(query_id, safe="")
        query = urllib.parse.urlencode({"start_cursor": cursor, "page_size": page_size})
        request = urllib.request.Request(
            f"https://api.notion.com/v1/views/{encoded_view}/queries/{encoded_query}?{query}",
            method="GET",
            headers=self._headers(),
        )
        return self._response(request)

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        encoded_page = urllib.parse.quote(page_id, safe="")
        request = urllib.request.Request(
            f"https://api.notion.com/v1/pages/{encoded_page}",
            method="GET",
            headers=self._headers(),
        )
        return self._response(request)


@dataclass(frozen=True)
class TotalViewFields:
    done: str
    categories: str
    timeboxing: str
    date_anchor: str
    category_relations: Mapping[str, str] = field(default_factory=dict)


def _text(prop: dict[str, Any]) -> str:
    prop_type = prop.get("type")
    if prop_type not in {"rich_text", "title"}:
        raise TotalViewError("timeboxing field is not structured text")
    pieces = prop.get(prop_type)
    if not isinstance(pieces, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("plain_text"), str)
        for item in pieces
    ):
        raise TotalViewError("timeboxing field has a malformed structured text payload")
    return "".join(item["plain_text"] for item in pieces)


def _categories(prop: dict[str, Any], relation_names: Mapping[str, str]) -> tuple[str, ...]:
    prop_type = prop.get("type")
    if prop_type == "multi_select":
        selected = prop.get("multi_select")
        if not isinstance(selected, list) or any(
            not isinstance(option, dict) or not isinstance(option.get("name"), str)
            for option in selected
        ):
            raise TotalViewError("category field has a malformed multi_select payload")
        return tuple(option["name"] for option in selected)
    if prop_type == "select":
        selected = prop.get("select")
        if selected is None:
            return ()
        if not isinstance(selected, dict) or not isinstance(selected.get("name"), str):
            raise TotalViewError("category field has a malformed select payload")
        return (selected["name"],)
    if prop_type == "relation":
        relations = prop.get("relation")
        if not isinstance(relations, list) or any(
            not isinstance(relation, dict) or not isinstance(relation.get("id"), str)
            for relation in relations
        ):
            raise TotalViewError("category field has a malformed relation payload")
        if "has_more" in prop and type(prop["has_more"]) is not bool:
            raise TotalViewError("category field has a malformed relation payload")
        if prop.get("has_more") is True:
            raise TotalViewError("truncated relation category is incomplete")
        categories = []
        for relation in relations:
            relation_id = relation["id"]
            if relation_id not in relation_names:
                raise TotalViewError("unmapped category relation")
            categories.append(relation_names[relation_id])
        return tuple(categories)
    raise TotalViewError("category field has an unsupported structured type")


def _date_anchor(prop: dict[str, Any]) -> date | None:
    prop_type = prop.get("type")
    if prop_type == "date":
        if "date" not in prop:
            raise TotalViewError("date anchor has a malformed date payload")
        value = prop["date"]
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TotalViewError("date anchor has a malformed date payload")
    elif prop_type in {"rich_text", "title"}:
        pieces = prop.get(prop_type)
        if not isinstance(pieces, list) or any(not isinstance(piece, dict) for piece in pieces):
            raise TotalViewError("date anchor has a malformed mention container")
        mentions = []
        for piece in pieces:
            if piece.get("type") != "mention":
                continue
            mention = piece.get("mention")
            if not isinstance(mention, dict):
                raise TotalViewError("date anchor has a malformed date mention")
            if mention.get("type") != "date":
                continue
            value = mention.get("date")
            if not isinstance(value, dict):
                raise TotalViewError("date anchor has a malformed date mention")
            mentions.append(value)
        if len(mentions) > 1:
            raise TotalViewError("date anchor contains multiple structured dates")
        if not mentions:
            return None
        value = mentions[0]
    else:
        raise TotalViewError("date anchor field has an unsupported structured type")
    start = value.get("start") if isinstance(value, dict) else None
    if not isinstance(start, str):
        raise TotalViewError("date anchor is missing a structured start value")
    try:
        return date.fromisoformat(start[:10])
    except ValueError as exc:
        raise TotalViewError("date anchor has an invalid structured start value") from exc


def _item(page: dict[str, Any], fields: TotalViewFields) -> TotalItem:
    properties = page.get("properties") if isinstance(page, dict) else None
    field_names = (fields.done, fields.categories, fields.timeboxing, fields.date_anchor)
    if (
        page.get("object") != "page"
        or not isinstance(properties, dict)
        or any(not isinstance(properties.get(name), dict) for name in field_names)
    ):
        raise TotalViewError("page field contract is incomplete")
    done_property = properties[fields.done]
    if done_property.get("type") != "checkbox" or type(done_property.get("checkbox")) is not bool:
        raise TotalViewError("page field contract requires a checkbox done field")
    return TotalItem(
        date_anchor=_date_anchor(properties[fields.date_anchor]),
        done=done_property["checkbox"],
        categories=_categories(properties[fields.categories], fields.category_relations),
        timeboxing=_text(properties[fields.timeboxing]),
    )


def _require_complete(response: dict[str, Any]) -> None:
    if not isinstance(response, dict):
        raise TotalViewError("view query response contract is incomplete")
    status = response.get("request_status")
    if status is not None and (
        not isinstance(status, dict) or status.get("type") != "complete"
    ):
        raise TotalViewError("view query is not complete")


def _query_identity(response: dict[str, Any], view_id: str) -> str:
    query_id = response.get("id")
    if (
        response.get("object") != "view_query"
        or not isinstance(query_id, str)
        or not query_id.strip()
        or response.get("view_id") != view_id
    ):
        raise TotalViewError("view query identity is missing or changed")
    return query_id


def _require_expiry(response: dict[str, Any]) -> None:
    expires_at = response.get("expires_at")
    if not isinstance(expires_at, str):
        raise TotalViewError("view query expires_at is missing or invalid")
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TotalViewError("view query expires_at is missing or invalid") from exc
    if parsed.tzinfo is None:
        raise TotalViewError("view query expires_at is missing or invalid")


def _require_page_identity(response: dict[str, Any], view_id: str, query_id: str) -> None:
    if response.get("view_id", view_id) != view_id or response.get("query_id", query_id) != query_id:
        raise TotalViewError("view query identity is missing or changed")


def _require_page_response(response: dict[str, Any]) -> None:
    if response.get("object") != "list" or response.get("type") != "page":
        raise TotalViewError("view query response contract is incomplete")
    if response.get("page") != {}:
        raise TotalViewError("view query response requires an empty page object")


def _require_terminal_cursor(response: dict[str, Any]) -> None:
    if response["has_more"] is False and (
        "next_cursor" not in response or response["next_cursor"] is not None
    ):
        raise TotalViewError("terminal view query page requires a null next_cursor")


def _require_anchor_order(items: list[TotalItem]) -> None:
    previous = None
    for row_index, item in enumerate(items):
        if item.date_anchor is None:
            continue
        if previous is not None and item.date_anchor > previous:
            raise TotalViewError(
                f"date anchors must be non-increasing before aggregation at row {row_index}"
            )
        previous = item.date_anchor


def total_from_view(
    api: Any,
    *,
    view_id: str,
    target_month: date,
    fields: TotalViewFields,
) -> TotalResult:
    if not isinstance(view_id, str) or not view_id.strip():
        raise TotalViewError("view_id must be provided explicitly")
    try:
        response = api.create_view_query(view_id, page_size=100)
    except Exception as exc:
        raise TotalViewError("view query request failed") from exc
    _require_complete(response)
    query_id = _query_identity(response, view_id)
    _require_expiry(response)
    total_count = response.get("total_count")
    if type(total_count) is not int or total_count < 0:
        raise TotalViewError("view query response is missing a valid total_count")
    if not isinstance(response.get("results"), list) or type(response.get("has_more")) is not bool:
        raise TotalViewError("view query response contract is incomplete")
    _require_terminal_cursor(response)
    pages = list(response["results"])
    seen_cursors: set[str] = set()
    while response["has_more"]:
        cursor = response.get("next_cursor")
        if not isinstance(cursor, str) or not cursor.strip():
            raise TotalViewError("view query has_more requires a valid next_cursor")
        if cursor in seen_cursors:
            raise TotalViewError("view query returned a repeated cursor")
        seen_cursors.add(cursor)
        try:
            response = api.get_view_query_results(
                view_id,
                query_id,
                cursor,
                page_size=100,
            )
        except Exception as exc:
            raise TotalViewError("view query request failed") from exc
        _require_page_response(response)
        _require_page_identity(response, view_id, query_id)
        _require_complete(response)
        if not isinstance(response.get("results"), list) or type(response.get("has_more")) is not bool:
            raise TotalViewError("view query response contract is incomplete")
        _require_terminal_cursor(response)
        pages.extend(response["results"])
    if len(pages) != total_count:
        raise TotalViewError("view query result count does not match total_count")
    page_ids = []
    seen_page_ids: set[str] = set()
    for reference in pages:
        page_id = reference.get("id") if isinstance(reference, dict) else None
        if (
            not isinstance(reference, dict)
            or reference.get("object") != "page"
            or not isinstance(page_id, str)
            or not page_id.strip()
        ):
            raise TotalViewError("view query returned an invalid page reference")
        if page_id in seen_page_ids:
            raise TotalViewError("view query returned a repeated page reference")
        seen_page_ids.add(page_id)
        page_ids.append(page_id)
    records = []
    for page_id in page_ids:
        try:
            record = api.retrieve_page(page_id)
        except Exception as exc:
            raise TotalViewError("page retrieve request failed") from exc
        if not isinstance(record, dict) or record.get("id") != page_id:
            raise TotalViewError("retrieved page identity is missing or changed")
        records.append(record)
    items = [_item(page, fields) for page in records]
    _require_anchor_order(items)
    return total(items, target_month)
