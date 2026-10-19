from typing import Annotated

from fastapi import Depends, Request

from muliao.container import Container


def _container(request: Request) -> Container:
    return request.app.state.container


C = Annotated[Container, Depends(_container)]
