// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Stand-in for a real ERC-20 on the anvil chains: run.py puts its runtime code at the real token
// address (anvil_setCode), so wallet and device see the usual contract call. Every holder has a
// large balance and transfers always succeed; nothing is stored.
contract TestToken {
    event Transfer(address indexed from, address indexed to, uint256 value);

    function name() external pure returns (string memory) { return "Test Token"; }
    function symbol() external pure returns (string memory) { return "TEST"; }
    function decimals() external pure returns (uint8) { return 6; }
    function totalSupply() external pure returns (uint256) { return 1e30; }
    function balanceOf(address) external pure returns (uint256) { return 1e18; }
    function allowance(address, address) external pure returns (uint256) { return 0; }

    function transfer(address to, uint256 value) external returns (bool) {
        emit Transfer(msg.sender, to, value);
        return true;
    }

    function approve(address, uint256) external pure returns (bool) { return true; }
}
