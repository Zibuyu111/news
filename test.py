from dataclasses import dataclass
import re

# traditional approach
class Point:
    def __init__(self,x: int,y: int) -> None:
        self.x = x
        self.y = y

    def __repr__(self) -> str:
        return f"Point(x={self.x}, y={self.y}])"

    def __eq__(self, other):
        if not isinstance(other, Point):
            return False
        return self.x == other.x and self.y == other.y
        
@dataclass()
class User:
    id: int
    name: str



@dataclass(frozen=True)
