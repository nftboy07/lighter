#!/usr/bin/env python3
"""
Early Detection Engine - Combines Mempool + Event Monitoring
=============================================================
Detects B20 pools via multiple signals:
1. Mempool TX monitoring (5-30s before mining)
2. Blockchain event listening (after mining)
3. Gas price analysis for optimal entry timing
"""

import asyncio
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Callable, Optional, List, Set
from dataclasses import dataclass

from eth_utils import to_checksum_address, to_bytes, keccak
import eth_abi

def predict_create2_address(deployer: str, salt: bytes, bytecode_hash: bytes) -> str:
    """
    Predict address of contract deployed via CREATE2.
    Formula: keccak256(0xff + deployer + salt + keccak256(bytecode))[12:]
    """
    deployer_bytes = to_bytes(hexstr=to_checksum_address(deployer))
    preimage = b'\xff' + deployer_bytes + salt + bytecode_hash
    hash_result = keccak(preimage)
    return to_checksum_address(hash_result[12:])

def predict_uniswap_v3_pool_address(
    factory: str, token_a: str, token_b: str, fee: int,
    init_code_hash: str = "0xe34f199b19b2b4f47f68442619d555527d244778a349b181734800e791054366"
) -> str:
    """Predict address of a Uniswap V3 pool before deployment."""
    t0 = min(token_a.lower(), token_b.lower())
    t1 = max(token_a.lower(), token_b.lower())
    
    encoded = eth_abi.encode(['address', 'address', 'uint24'], [to_checksum_address(t0), to_checksum_address(t1), fee])
    salt = keccak(encoded)
    
    deployer_bytes = to_bytes(hexstr=to_checksum_address(factory))
    bytecode_hash = to_bytes(hexstr=init_code_hash)
    
    preimage = b'\xff' + deployer_bytes + salt + bytecode_hash
    hash_result = keccak(preimage)
    return to_checksum_address(hash_result[12:])

logger = logging.getLogger(__name__)


@dataclass
class DetectionSignal:
    """Early detection signal."""
    signal_type: str  # 'mempool_pending' | 'event_mined' | 'gas_spike'
    token_address: Optional[str]
    pool_address: Optional[str]
    confidence: float  # 0-1
    detected_at: float  # Unix timestamp
    details: Dict


class EarlyDetectionEngine:
    """Combines multiple detection signals for earliest pool detection."""

    def __init__(self, db_manager=None):
        self.db = db_manager
        self.mempool_monitor = None
        self.event_monitor = None
        self.detection_signals: List[DetectionSignal] = []
        self.callbacks: List[Callable] = []
        self.is_running = False
        self.stats = {
            'mempool_detections': 0,
            'event_detections': 0,
            'avg_mempool_lead_time': 0,
            'false_positives': 0,
            'confirmed_pools': 0,
        }

    def register_callback(self, callback: Callable):
        """Register callback for when pool is detected."""
        self.callbacks.append(callback)

    async def on_mempool_tx(self, event_type: str, data: Dict):
        """Handle mempool transaction detection."""
        logger.info(f"🔔 Mempool signal: {event_type}")
        
        token_address = data.get('token_address')
        if self.mempool_monitor and token_address:
            is_sandwiched, _, details_str = self.mempool_monitor.is_token_sandwiched(token_address)
            if is_sandwiched:
                logger.warning(f"⚠️ Mempool token {token_address} is sandwiched: {details_str}")
                data['mev_warning'] = details_str
                
        signal = DetectionSignal(
            signal_type='mempool_pending',
            token_address=token_address,
            pool_address=None,  # Not known until TX mines
            confidence=0.5 if 'mev_warning' in data else 0.7,  # Reduce confidence if sandwiched
            detected_at=time.time(),
            details=data
        )
        
        self.detection_signals.append(signal)
        self.stats['mempool_detections'] += 1
        
        # Notify immediately (high confidence mempool TX)
        await self._notify_detection(signal)

    async def on_pool_created(self, event_type: str, data: Dict):
        """Handle blockchain pool creation event."""
        pool_addr = data.get('pool_address')
        logger.info(f"✅ Pool created on-chain: {pool_addr[:8]}...")
        
        # Check if we detected this in mempool first
        mempool_signal = None
        for sig in self.detection_signals:
            if sig.signal_type == 'mempool_pending' and sig.pool_address == pool_addr:
                mempool_signal = sig
                break
        
        signal = DetectionSignal(
            signal_type='event_mined',
            token_address=data.get('token_address'),
            pool_address=pool_addr,
            confidence=1.0,  # Confirmed on-chain
            detected_at=time.time(),
            details=data
        )
        
        # Calculate mempool lead time
        if mempool_signal:
            lead_time = signal.detected_at - mempool_signal.detected_at
            logger.info(f"⏱️  Mempool lead time: {lead_time:.2f} seconds")
            self._update_avg_lead_time(lead_time)
        
        self.detection_signals.append(signal)
        self.stats['event_detections'] += 1
        self.stats['confirmed_pools'] += 1
        
        # Notify
        await self._notify_detection(signal)

    async def _notify_detection(self, signal: DetectionSignal):
        """Notify all callbacks of detection."""
        for callback in self.callbacks:
            try:
                await callback('pool_detected', {
                    'signal_type': signal.signal_type,
                    'token': signal.token_address,
                    'pool': signal.pool_address,
                    'confidence': signal.confidence,
                    'timestamp': datetime.fromtimestamp(signal.detected_at).isoformat(),
                    'details': signal.details,
                })
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _update_avg_lead_time(self, lead_time: float):
        """Update average mempool lead time."""
        if self.stats['mempool_detections'] == 0:
            self.stats['avg_mempool_lead_time'] = lead_time
        else:
            current_avg = self.stats['avg_mempool_lead_time']
            new_count = self.stats['mempool_detections']
            self.stats['avg_mempool_lead_time'] = (
                (current_avg * (new_count - 1) + lead_time) / new_count
            )

    def get_stats(self) -> Dict:
        """Get detection statistics."""
        return {
            **self.stats,
            'total_signals': len(self.detection_signals),
        }


class MultiSourceDetector:
    """High-level detector combining mempool + events + gas trends."""

    def __init__(self, mempool_monitor, event_monitor, db_manager=None):
        self.mempool = mempool_monitor
        self.events = event_monitor
        self.db = db_manager
        self.early_detection = EarlyDetectionEngine(db_manager)
        self.early_detection.mempool_monitor = mempool_monitor
        self.is_running = False

    def register_detection_callback(self, callback: Callable):
        """Register callback for when pool is detected."""
        self.early_detection.register_callback(callback)

    async def start(self):
        """Start all detection sources."""
        logger.info("🚀 Starting multi-source detection engine...")
        self.is_running = True
        
        # Register callbacks
        self.mempool.register_callback(self.early_detection.on_mempool_tx)
        self.events.register_callback(self.early_detection.on_pool_created)
        
        # Start both monitors
        mempool_task = asyncio.create_task(self.mempool.start())
        events_task = asyncio.create_task(
            self.events.listen_pool_created(self.early_detection.on_pool_created)
        )
        
        await asyncio.gather(mempool_task, events_task)

    def stop(self):
        """Stop detection."""
        logger.info("⏹ Stopping multi-source detector...")
        self.is_running = False
        self.mempool.stop()

    def get_stats(self) -> Dict:
        """Get overall statistics."""
        return {
            'is_running': self.is_running,
            'detection_engine': self.early_detection.get_stats(),
            'mempool_monitor': self.mempool.get_stats() if self.mempool else {},
        }


class SocialSignalParser:
    """Parses social messages/casts for contract addresses and launch signals."""

    @staticmethod
    def extract_contract_addresses(text: str) -> List[str]:
        """Extract Ethereum checksummed contract addresses from text."""
        import re
        pattern = r"0x[a-fA-F0-9]{40}"
        matches = re.findall(pattern, text)
        extracted = []
        for m in matches:
            try:
                extracted.append(to_checksum_address(m))
            except Exception:
                pass
        return list(set(extracted))

    @staticmethod
    def contains_alpha_keywords(text: str) -> bool:
        """Check if social post text contains high-intent alpha keywords."""
        keywords = ["launch", "live on base", "pool created", "fair launch", "o1.exchange", "cc0.company", "b20"]
        lowered = text.lower()
        return any(k in lowered for k in keywords)


if __name__ == '__main__':
    """Test the detection engine."""
    logging.basicConfig(level=logging.INFO)
    
    async def test_callback(event_type: str, data: Dict):
        logger.info(f"Detection callback: {event_type} - {data}")
    
    async def main():
        from mempool_monitor import MempoolMonitor
        from event_monitor import EventMonitor
        
        mempool = MempoolMonitor()
        events = EventMonitor(None, None, None, None)
        
        detector = MultiSourceDetector(mempool, events)
        detector.register_detection_callback(test_callback)
        
        try:
            await detector.start()
        except KeyboardInterrupt:
            detector.stop()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
