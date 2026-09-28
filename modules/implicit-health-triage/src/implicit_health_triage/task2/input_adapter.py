from .schemas import NormalizedTurn, TriageInput


class InputAdapter:
    def adapt(self, input_data: TriageInput) -> NormalizedTurn | None:
        if not input_data.is_final or not input_data.text.strip():
            return None
        return NormalizedTurn(
            session_id=input_data.session_id,
            turn_id=input_data.turn_id,
            text=input_data.text.strip(),
        )
