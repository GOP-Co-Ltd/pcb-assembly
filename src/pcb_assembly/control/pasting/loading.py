"""対話的なペーストローディングユーティリティ."""

from pcb_assembly.control.pasting.applicator import PasteApplicator


def interactive_loading(applicator: PasteApplicator, default_amount: float) -> None:
    """対話的にペーストをローディングする.

    Args:
        applicator: ペーストアプリケーター
        default_amount: デフォルトの押し出し量 [μL]
    """
    print(f"ローディング (デフォルト量: {default_amount} μL)")
    print("Enter: デフォルト量を押し出し, 数値: その量を押し出し, q: 終了")
    print()

    while True:
        cmd = input("> ").strip()

        match cmd:
            case "q" | "quit":
                break
            case "":
                print(f"ローディング: {default_amount} μL")
                applicator.load(default_amount)
                print("完了")
            case _:
                try:
                    amount = float(cmd)
                except ValueError:
                    print("不正な入力です")
                    continue
                print(f"ローディング: {amount} μL")
                applicator.load(amount)
                print("完了")
