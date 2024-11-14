# /// script
# dependencies = ["web3", "rich", "eth-tester", "py-evm", "prefect"]
# ///

from random import choice, uniform
from time import sleep

from eth_account import Account
from eth_tester import PyEVMBackend
from prefect import flow, task
from prefect.cache_policies import NONE
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from rich.console import Console
from rich.table import Table
from web3 import EthereumTesterProvider, Web3

console = Console()
w3 = Web3(EthereumTesterProvider(PyEVMBackend()))


class SimpleToken(BaseModel):
    balances: dict = Field(default_factory=dict)

    def transfer(self, from_addr, to_addr, amount):
        if self.balances.get(from_addr, 0) >= amount:
            self.balances[from_addr] -= amount
            self.balances[to_addr] = self.balances.get(to_addr, 0) + amount
            return True
        else:
            return False

    def mint(self, to_addr, amount):
        self.balances[to_addr] = self.balances.get(to_addr, 0) + amount


class Network(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    peers: list[Account] = Field(
        default_factory=lambda: [Account.create() for _ in range(5)]
    )
    total_transactions: int = Field(default=0)
    total_volume: float = Field(default=0)
    token: SimpleToken = Field(default_factory=SimpleToken)

    _transactions: list = PrivateAttr(default_factory=list)

    def model_post_init(self, __context):
        assert __context is None
        self.fund_peers()
        self.distribute_tokens()

    def fund_peers(self):
        coinbase_balance = w3.eth.get_balance(w3.eth.accounts[0])
        if coinbase_balance == 0:
            w3.eth.send_transaction(
                {
                    "from": w3.eth.accounts[0],
                    "to": w3.eth.accounts[0],
                    "value": w3.to_wei(100, "ether"),
                }
            )

        for peer in self.peers:
            try:
                amount = w3.to_wei(uniform(5, 15), "ether")
                tx_hash = w3.eth.send_transaction(
                    {
                        "from": w3.eth.accounts[0],
                        "to": peer.address,
                        "value": amount,
                    }
                )
                w3.eth.wait_for_transaction_receipt(tx_hash)
            except Exception as e:
                console.print(f"[red]Error funding peer {peer.address}: {str(e)}[/red]")

    def distribute_tokens(self):
        for peer in self.peers:
            self.token.mint(peer.address, uniform(100, 500))

    def record_transaction(self, sender: str, receiver: str, amount: float):
        self._transactions.append((sender, receiver, amount))

    def random_token_transaction(self):
        sender = choice(self.peers)
        receiver = choice([p for p in self.peers if p != sender])
        amount = round(uniform(1, 50), 2)

        if self.token.transfer(sender.address, receiver.address, amount):
            self.total_transactions += 1
            self.total_volume += amount
            self.record_transaction(sender.address, receiver.address, amount)
            return sender, receiver, amount
        else:
            return None


def format_address(addr):
    return f"{addr[:6]}...{addr[-4:]}"


@task(cache_policy=NONE)
def show_network_state(network: Network):
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Peer", style="cyan")
    table.add_column("ETH Balance", style="green")
    table.add_column("Token Balance", style="yellow")

    for p in sorted(
        network.peers, key=lambda x: w3.eth.get_balance(x.address), reverse=True
    ):
        eth_balance = w3.from_wei(w3.eth.get_balance(p.address), "ether")
        token_balance = network.token.balances.get(p.address, 0)
        table.add_row(
            f"{format_address(p.address)}",
            f"{float(eth_balance):.3f} ETH",
            f"{token_balance:.2f} Tokens",
        )

    table.add_row("", "", "")
    table.add_row(
        "[bold]Network Stats[/bold]",
        f"Txs: {network.total_transactions}",
        f"Vol: {network.total_volume:.2f} Tokens",
    )
    console.print(table)


@task(cache_policy=NONE)
def process_network_activity(network: Network, tick: int):
    result = network.random_token_transaction()
    if result is None:
        console.print("[yellow]Skip: Insufficient tokens for transaction[/yellow]")
        return

    sender, receiver, amount = result
    console.print(
        f"[green]✓[/green] {format_address(sender.address)} "
        f"→ {amount:.2f} Tokens → "
        f"{format_address(receiver.address)}"
    )


@task(cache_policy=NONE)
def visualize_transactions(network: Network):
    # For simplicity, we'll just print the transactions here
    console.print("\n[bold blue]Transaction Graph[/bold blue]")
    for idx, (sender, receiver, amount) in enumerate(network.transactions):
        console.print(
            f"{idx+1}. {format_address(sender)} "
            f"→ {amount:.2f} Tokens → "
            f"{format_address(receiver)}"
        )


@flow(name="P2P Network Simulation")
def run_network(network: Network, limit: int | None = None):
    tick = 0

    while tick < (limit or float("inf")):
        tick += 1
        process_network_activity(network, tick)
        if tick % 5 == 0:
            show_network_state(network)
        sleep(1)

    # Visualize transactions at the end
    visualize_transactions(network)


@flow(name="Observe P2P Network")
def observe_network(limit: int | None = None):
    network = Network()
    console.print("[bold blue]Ethereum P2P Network Simulator with Tokens[/bold blue]")
    show_network_state(network)

    try:
        run_network(network, limit)
    except KeyboardInterrupt:
        console.print("\n[bold red]Simulation stopped[/bold red]")
        show_network_state(network)


if __name__ == "__main__":
    observe_network(limit=10)
