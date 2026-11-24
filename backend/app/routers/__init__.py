"""路由汇总：语义化结案路由必须先于动态资源路由注册。"""
from __future__ import annotations

from app.routers import retry_closure
from app.routers import resource
from app.routers import chains

ROUTERS = [retry_closure, resource, chains]
