class GraphEngineeringError(Exception):
    pass


class GraphValidationError(GraphEngineeringError):
    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


class ContractViolation(GraphEngineeringError):
    pass


class MergeRejectedError(ContractViolation):
    pass


class RetryableNodeError(GraphEngineeringError):
    pass


class AgentExecutionError(GraphEngineeringError):
    pass


class AgentProtocolError(ContractViolation):
    pass


class AgentRateLimitError(RetryableNodeError):
    pass


class AgentTimeoutError(RetryableNodeError):
    pass


class EffectIndeterminateError(GraphEngineeringError):
    pass
