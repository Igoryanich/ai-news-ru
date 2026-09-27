from .base import CollectResult, FetchError, RawItem, SourceSpec
from .impl import COLLECTORS, fetch_article_text

__all__ = ["COLLECTORS", "CollectResult", "FetchError", "RawItem", "SourceSpec", "fetch_article_text"]
