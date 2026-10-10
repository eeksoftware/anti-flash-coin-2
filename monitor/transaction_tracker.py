from models.transaction import TokenTransfer


class TransactionTracker:
    """Keep seen transfer events in memory and return only first sightings."""

    def __init__(self) -> None:
        self._seen_event_ids: set[tuple[str, str, str]] = set()

    def has_seen(self, transfer: TokenTransfer) -> bool:
        return transfer.event_id in self._seen_event_ids

    def reset(self) -> None:
        self._seen_event_ids.clear()

    def add_new(self, transfers: list[TokenTransfer]) -> list[TokenTransfer]:
        new_transfers: list[TokenTransfer] = []
        for transfer in transfers:
            if transfer.event_id in self._seen_event_ids:
                continue
            self._seen_event_ids.add(transfer.event_id)
            new_transfers.append(transfer)
        return new_transfers
