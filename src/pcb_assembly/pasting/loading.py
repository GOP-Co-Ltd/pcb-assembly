"""対話的なペーストローディングユーティリティ."""

import logging

from pcb_assembly.pasting.applicator import PasteApplicator

logger = logging.getLogger(__name__)


def interactive_loading(applicator: PasteApplicator, default_amount: float) -> None:
    """対話的にペーストをローディングする.

    Args:
        applicator: ペーストアプリケーター
        default_amount: デフォルトの押し出し量 [μL]
    """
    logger.info("ローディング (デフォルト量: %s μL)", default_amount)
    logger.info("Enter: デフォルト量を押し出し, 数値: その量を押し出し, q: 終了")

    while True:
        cmd = input("> ").strip()

        match cmd:
            case "q" | "quit":
                break
            case "":
                logger.info("ローディング: %s μL", default_amount)
                applicator.load(default_amount)
                logger.info("完了")
            case _:
                try:
                    amount = float(cmd)
                except ValueError:
                    logger.warning("不正な入力です")
                    continue
                logger.info("ローディング: %s μL", amount)
                applicator.load(amount)
                logger.info("完了")
