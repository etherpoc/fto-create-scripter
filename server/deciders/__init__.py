"""
deciders 配下の戦略モジュールをすべて import してレジストリに自己登録させる。
新しい戦略を追加するときはここに 1 行追加する (またはこのファイルで動的 import)。
"""

from . import zigzag_line_break  # noqa: F401
from . import zigzag_ai  # noqa: F401
