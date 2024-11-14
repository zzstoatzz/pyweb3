# /// script
# dependencies = ["web3", "rich", "eth-tester", "py-evm", "prefect"]
# ///

import logging
from decimal import Decimal
from random import choice, uniform
from time import sleep

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_tester import PyEVMBackend
from prefect import flow, task
from prefect.cache_policies import NONE
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from rich import box
from rich.console import Console
from rich.table import Table
from web3 import EthereumTesterProvider, Web3

# Set logging level to WARNING to reduce verbosity
logging.getLogger("prefect").setLevel(logging.WARNING)

console = Console()
w3 = Web3(EthereumTesterProvider(PyEVMBackend()))
w3.eth.default_account = w3.eth.accounts[0]  # Set the default account for gas payments


class SimpleToken(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str = "SimToken"
    symbol: str = "SIM"
    balances: dict[str, Decimal] = Field(default_factory=dict)

    def transfer(self, from_addr: str, to_addr: str, amount: float) -> bool:
        amount_decimal = Decimal(str(amount))
        if self.balances.get(from_addr, Decimal("0")) >= amount_decimal:
            self.balances[from_addr] -= amount_decimal
            self.balances[to_addr] = (
                self.balances.get(to_addr, Decimal("0")) + amount_decimal
            )
            return True
        else:
            return False

    def mint(self, to_addr: str, amount: float):
        amount_decimal = Decimal(str(amount))
        self.balances[to_addr] = (
            self.balances.get(to_addr, Decimal("0")) + amount_decimal
        )


class Peer(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    account: LocalAccount
    desired_ratio: float = Field(
        default=0.5
    )  # Desired ratio of tokens to total holdings
    utility: float = Field(default=0.0)

    def update_utility(self, eth_balance: Decimal, token_balance: float):
        token_balance_decimal = Decimal(str(token_balance))
        total = eth_balance + token_balance_decimal
        if total == 0:
            self.utility = 0
            return
        current_ratio = token_balance_decimal / total
        # Utility is maximized when current_ratio equals desired_ratio
        self.utility = -abs(self.desired_ratio - float(current_ratio))


class Network(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    peers: list[Peer] = Field(
        default_factory=lambda: [Peer(account=Account.create()) for _ in range(5)]
    )
    token: SimpleToken = Field(default_factory=SimpleToken)
    _transactions: list = PrivateAttr(default_factory=list)

    def model_post_init(self, __context):
        self.fund_peers()
        self.distribute_tokens()

    def fund_peers(self):
        # Fund peers with ETH
        coinbase = w3.eth.accounts[0]
        coinbase_balance = w3.eth.get_balance(coinbase)
        if coinbase_balance == 0:
            # If coinbase has no ETH, mine some
            w3.provider.ethereum_tester.mine_blocks(1)
        nonce = w3.eth.get_transaction_count(coinbase)
        for idx, peer in enumerate(self.peers):
            try:
                amount = w3.to_wei(uniform(5, 15), "ether")
                tx = {
                    "from": coinbase,
                    "to": peer.account.address,
                    "value": amount,
                    "gas": 21000,
                    "gasPrice": w3.eth.gas_price,
                    "nonce": nonce + idx,
                    "chainId": w3.eth.chain_id,
                }
                tx_hash = w3.eth.send_transaction(tx)
                # Mine a block to confirm the transaction
                w3.provider.ethereum_tester.mine_blocks(1)
                w3.eth.wait_for_transaction_receipt(tx_hash)
            except Exception as e:
                console.log(
                    f"[red]Error funding peer {peer.account.address}: {str(e)}[/red]"
                )

    def distribute_tokens(self):
        # Mint and distribute tokens to peers
        for peer in self.peers:
            amount = uniform(50, 150)
            self.token.mint(peer.account.address, amount)

    def peer_trade(self, sender: Peer):
        # Select a receiver randomly
        receiver = choice([p for p in self.peers if p != sender])

        # Get balances as Decimal for consistency
        sender_eth_balance = w3.from_wei(
            w3.eth.get_balance(sender.account.address), "ether"
        )
        sender_token_balance = Decimal(
            str(self.token.balances.get(sender.account.address, 0))
        )
        receiver_eth_balance = w3.from_wei(
            w3.eth.get_balance(receiver.account.address), "ether"
        )
        receiver_token_balance = Decimal(
            str(self.token.balances.get(receiver.account.address, 0))
        )

        # Decide whether to trade ETH for tokens or tokens for ETH
        sender_total = sender_eth_balance + sender_token_balance
        if sender_total == 0:
            return  # Cannot trade if total holdings are zero
        sender_current_ratio = sender_token_balance / sender_total

        if sender_current_ratio < Decimal(str(sender.desired_ratio)):
            # Sender wants more tokens; offer ETH for tokens
            max_eth_to_send = sender_eth_balance * Decimal("0.1")  # Limit trade size
            if max_eth_to_send < Decimal("0.01"):
                return  # Not enough ETH to trade
            eth_amount = Decimal(str(round(uniform(0.01, float(max_eth_to_send)), 4)))
            # Check if receiver has enough tokens
            if receiver_token_balance < eth_amount:
                return  # Receiver doesn't have enough tokens
            # Simulate ETH transfer from sender to receiver
            if self.transfer_eth(sender, receiver, eth_amount):
                # Simulate token transfer from receiver to sender
                token_amount = eth_amount  # Simple 1:1 exchange rate for simplicity
                if self.transfer_token(receiver, sender, float(token_amount)):
                    self.record_transaction(
                        sender.account.address,
                        receiver.account.address,
                        "ETH→Token",
                        float(eth_amount),
                    )
                    console.log(
                        f"[green]Trade:[/green] {format_address(sender.account.address)} sent {eth_amount} ETH "
                        f"to {format_address(receiver.account.address)} for {token_amount} Tokens"
                    )
        else:
            # Sender wants more ETH; offer tokens for ETH
            max_tokens_to_send = sender_token_balance * Decimal("0.1")
            if max_tokens_to_send < Decimal("1"):
                return  # Not enough tokens to trade
            token_amount = Decimal(str(round(uniform(1, float(max_tokens_to_send)), 2)))
            # Check if receiver has enough ETH
            if receiver_eth_balance < token_amount:
                return  # Receiver doesn't have enough ETH
            # Simulate token transfer from sender to receiver
            if self.transfer_token(sender, receiver, float(token_amount)):
                # Simulate ETH transfer from receiver to sender
                eth_amount = token_amount  # Simple 1:1 exchange rate for simplicity
                if self.transfer_eth(receiver, sender, eth_amount):
                    self.record_transaction(
                        sender.account.address,
                        receiver.account.address,
                        "Token→ETH",
                        float(eth_amount),
                    )
                    console.log(
                        f"[green]Trade:[/green] {format_address(sender.account.address)} sent {token_amount} Tokens "
                        f"to {format_address(receiver.account.address)} for {eth_amount} ETH"
                    )

    def transfer_eth(self, from_peer: Peer, to_peer: Peer, amount_eth: Decimal) -> bool:
        # Simulate ETH transfer between peers
        amount_wei = w3.to_wei(amount_eth, "ether")
        gas_price = w3.eth.gas_price
        gas_limit = 21000  # Standard ETH transfer gas limit
        gas_cost = gas_limit * gas_price
        from_balance = w3.eth.get_balance(from_peer.account.address)
        if from_balance < amount_wei + gas_cost:
            console.log(
                f"[yellow]Trade failed:[/yellow] {format_address(from_peer.account.address)} has insufficient ETH."
            )
            return False
        nonce = w3.eth.get_transaction_count(from_peer.account.address)
        tx = {
            "from": from_peer.account.address,
            "to": to_peer.account.address,
            "value": amount_wei,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        }
        signed_tx = w3.eth.account.sign_transaction(tx, from_peer.account.key)
        w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        # Mine a block to confirm the transaction
        w3.provider.ethereum_tester.mine_blocks(1)
        w3.eth.wait_for_transaction_receipt(signed_tx.hash)
        return True

    def transfer_token(self, from_peer: Peer, to_peer: Peer, amount: float) -> bool:
        return self.token.transfer(
            from_peer.account.address, to_peer.account.address, amount
        )

    def record_transaction(
        self, sender: str, receiver: str, tx_type: str, amount: float
    ):
        self._transactions.append((sender, receiver, tx_type, amount))


def format_address(addr):
    return f"{addr[:6]}...{addr[-4:]}"


def create_network_table(network: Network) -> Table:
    table = Table(show_header=True, header_style="bold magenta", box=box.SIMPLE)
    table.add_column("Peer", style="cyan", no_wrap=True)
    table.add_column("ETH Balance", style="green")
    table.add_column("Token Balance", style="yellow")
    table.add_column("Utility", style="red")

    for peer in network.peers:
        eth_balance = w3.from_wei(w3.eth.get_balance(peer.account.address), "ether")
        token_balance = network.token.balances.get(peer.account.address, Decimal("0"))
        peer.update_utility(eth_balance, float(token_balance))
        utility = peer.utility
        utility_str = f"{utility:.4f}"
        if utility == 0:
            utility_str = f"[bold green]{utility_str}[/bold green]"
        table.add_row(
            f"{format_address(peer.account.address)}",
            f"{eth_balance:.2f}",
            f"{float(token_balance):.2f}",
            utility_str,
        )
    return table


def _get_peer_trade_task_run_name(parameters):
    sender = parameters["sender"]
    return f"{format_address(sender.account.address)} trades"


@task(cache_policy=NONE)
def process_network_activity(network: Network):
    # Process trades sequentially to avoid nonce conflicts
    for sender in network.peers:
        peer_trade_task(network, sender)


@task(
    name="Peer Trade",
    task_run_name=_get_peer_trade_task_run_name,
    cache_policy=NONE,
)
def peer_trade_task(network: Network, sender: Peer):
    network.peer_trade(sender)


@flow(name="P2P Network Simulation")
def run_network(network: Network, limit: int | None = None):
    tick = 0
    while tick < (limit or float("inf")):
        tick += 1
        console.clear()
        console.print(f"[bold blue]Tick {tick}[/bold blue]\n")

        process_network_activity(network)

        console.print(create_network_table(network))

        sleep(1)

    visualize_transactions(network)


def visualize_transactions(network: Network):
    if network._transactions:
        tx_table = Table(show_header=True, header_style="bold magenta", box=box.SIMPLE)
        tx_table.add_column("#", style="dim", width=6)
        tx_table.add_column("Sender", style="cyan")
        tx_table.add_column("Receiver", style="cyan")
        tx_table.add_column("Type", style="yellow")
        tx_table.add_column("Amount", style="green")
        for idx, tx in enumerate(network._transactions, start=1):
            sender, receiver, tx_type, amount = tx
            tx_table.add_row(
                str(idx),
                format_address(sender),
                format_address(receiver),
                tx_type,
                f"{amount:.2f}",
            )
        console.print("\n[bold blue]Transaction History[/bold blue]")
        console.print(tx_table)
    else:
        console.print(
            "\n[bold blue]No transactions were made during the simulation.[/bold blue]"
        )


@flow(name="Observe P2P Network")
def observe_network(limit: int | None = None):
    network = Network()
    console.print(
        "[bold blue]Ethereum P2P Network Simulation with ETH and Token Trading[/bold blue]\n"
    )
    console.print(create_network_table(network))

    try:
        run_network(network, limit)
    except KeyboardInterrupt:
        console.print("\n[bold red]Simulation stopped[/bold red]")
        console.print(create_network_table(network))


if __name__ == "__main__":
    observe_network(limit=10)
