import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

class HallucinationAnalytics:
    def __init__(self, redis_service):
        self.redis_service = redis_service

    async def log_validation_result(self, conversation_id: str, user_id: str, validation_result: Dict, question: str = "", context_length: int = 0):
        """Log validation results for analytics"""
        try:
            log_entry = {
                'timestamp': datetime.utcnow().isoformat(),
                'conversation_id': conversation_id,
                'user_id': user_id,
                'question_preview': question[:100] if question else "",
                'context_length': context_length,
                'is_hallucination': validation_result.get('is_hallucination', False),
                'confidence_score': validation_result.get('confidence_score', 0.0),
                'warning_flags': validation_result.get('warning_flags', []),
                'methods': validation_result.get('methods', {}),
                'detection_scores': self.extract_detection_scores(validation_result)
            }
            
            # Store in Redis with expiration (30 days)
            key = f"validation_log:{datetime.utcnow().strftime('%Y-%m-%d')}:{conversation_id}:{datetime.utcnow().strftime('%H%M%S')}"
            await self.redis_service.redis.setex(
                key, 
                int(timedelta(days=30).total_seconds()), 
                json.dumps(log_entry)
            )
            
            # Also store daily summary
            await self.update_daily_summary(log_entry)
            
        except Exception as e:
            logger.error(f"Error logging validation result: {e}")

    def extract_detection_scores(self, validation_result: Dict) -> Dict:
        """Extract key detection scores for analytics"""
        scores = {}
        methods = validation_result.get('methods', {})
        
        if 'llm_detection' in methods:
            scores['llm_score'] = methods['llm_detection'].get('score', 0)
        
        if 'semantic_similarity' in methods:
            scores['similarity_score'] = methods['semantic_similarity'].get('similarity', 0)
        
        if 'context_grounding' in methods:
            scores['grounding_score'] = methods['context_grounding'].get('grounding_score', 0)
        
        if 'citation_verification' in methods:
            scores['citation_score'] = methods['citation_verification'].get('verification_ratio', 0)
        
        return scores

    async def update_daily_summary(self, log_entry: Dict):
        """Update daily summary statistics"""
        try:
            date_str = datetime.utcnow().strftime('%Y-%m-%d')
            summary_key = f"daily_summary:{date_str}"
            
            # Get existing summary or create new one
            existing_summary = await self.redis_service.redis.get(summary_key)
            
            if existing_summary:
                summary = json.loads(existing_summary)
            else:
                summary = {
                    'date': date_str,
                    'total_responses': 0,
                    'hallucination_count': 0,
                    'confidence_scores': [],
                    'warning_flags_count': {},
                    'method_scores': {
                        'llm_scores': [],
                        'similarity_scores': [],
                        'grounding_scores': [],
                        'citation_scores': []
                    }
                }
            
            # Update summary
            summary['total_responses'] += 1
            summary['confidence_scores'].append(log_entry['confidence_score'])
            
            if log_entry['is_hallucination']:
                summary['hallucination_count'] += 1
            
            # Count warning flags
            for flag in log_entry['warning_flags']:
                summary['warning_flags_count'][flag] = summary['warning_flags_count'].get(flag, 0) + 1
            
            # Update method scores
            detection_scores = log_entry['detection_scores']
            for score_type, scores_list in summary['method_scores'].items():
                score_key = score_type.replace('_scores', '_score')
                if score_key in detection_scores:
                    scores_list.append(detection_scores[score_key])
            
            # Store updated summary (expires in 31 days)
            await self.redis_service.redis.setex(
                summary_key,
                int(timedelta(days=31).total_seconds()),
                json.dumps(summary)
            )
            
        except Exception as e:
            logger.error(f"Error updating daily summary: {e}")

    async def get_hallucination_stats(self, days: int = 7) -> Dict:
        """Get hallucination statistics for the past N days"""
        try:
            stats = {
                'total_responses': 0,
                'hallucination_count': 0,
                'hallucination_rate': 0.0,
                'average_confidence': 0.0,
                'daily_breakdown': [],
                'common_warning_flags': {},
                'method_performance': {},
                'trends': {}
            }
            
            # Get daily summaries for the specified period
            end_date = datetime.utcnow()
            daily_data = []
            
            for i in range(days):
                date = end_date - timedelta(days=i)
                date_str = date.strftime('%Y-%m-%d')
                
                # Get daily summary
                summary_key = f"daily_summary:{date_str}"
                summary_data = await self.redis_service.redis.get(summary_key)
                
                if summary_data:
                    daily_summary = json.loads(summary_data)
                    daily_data.append(daily_summary)
                    
                    # Aggregate stats
                    stats['total_responses'] += daily_summary['total_responses']
                    stats['hallucination_count'] += daily_summary['hallucination_count']
                    
                    # Aggregate warning flags
                    for flag, count in daily_summary.get('warning_flags_count', {}).items():
                        stats['common_warning_flags'][flag] = stats['common_warning_flags'].get(flag, 0) + count
            
            # Calculate rates and averages
            if stats['total_responses'] > 0:
                stats['hallucination_rate'] = stats['hallucination_count'] / stats['total_responses']
            
            # Calculate method performance
            stats['method_performance'] = await self.calculate_method_performance(daily_data)
            
            # Calculate trends
            stats['trends'] = self.calculate_trends(daily_data)
            
            # Prepare daily breakdown
            stats['daily_breakdown'] = self.prepare_daily_breakdown(daily_data)
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting hallucination stats: {e}")
            return {'error': str(e)}

    async def calculate_method_performance(self, daily_data: List[Dict]) -> Dict:
        """Calculate performance metrics for each detection method"""
        performance = {
            'llm_detection': {'avg_score': 0, 'consistency': 0},
            'semantic_similarity': {'avg_score': 0, 'consistency': 0},
            'context_grounding': {'avg_score': 0, 'consistency': 0},
            'citation_verification': {'avg_score': 0, 'consistency': 0}
        }
        
        try:
            all_scores = {
                'llm_scores': [],
                'similarity_scores': [],
                'grounding_scores': [],
                'citation_scores': []
            }
            
            # Collect all scores
            for daily in daily_data:
                method_scores = daily.get('method_scores', {})
                for score_type, scores in method_scores.items():
                    all_scores[score_type].extend(scores)
            
            # Calculate averages and consistency
            for method, scores_key in [
                ('llm_detection', 'llm_scores'),
                ('semantic_similarity', 'similarity_scores'),
                ('context_grounding', 'grounding_scores'),
                ('citation_verification', 'citation_scores')
            ]:
                scores = all_scores[scores_key]
                if scores:
                    avg_score = sum(scores) / len(scores)
                    # Calculate standard deviation as consistency metric
                    variance = sum((x - avg_score) ** 2 for x in scores) / len(scores)
                    std_dev = variance ** 0.5
                    consistency = max(0, 1 - std_dev)  # Higher is more consistent
                    
                    performance[method] = {
                        'avg_score': round(avg_score, 3),
                        'consistency': round(consistency, 3),
                        'sample_size': len(scores)
                    }
            
        except Exception as e:
            logger.error(f"Error calculating method performance: {e}")
        
        return performance

    def calculate_trends(self, daily_data: List[Dict]) -> Dict:
        """Calculate trend analysis"""
        trends = {
            'hallucination_trend': 'stable',
            'confidence_trend': 'stable',
            'volume_trend': 'stable'
        }
        
        try:
            if len(daily_data) < 3:
                return trends
            
            # Sort by date
            daily_data.sort(key=lambda x: x['date'])
            
            # Calculate hallucination rate trend
            rates = []
            for daily in daily_data:
                if daily['total_responses'] > 0:
                    rate = daily['hallucination_count'] / daily['total_responses']
                    rates.append(rate)
            
            if len(rates) >= 3:
                # Simple trend analysis: compare first third vs last third
                first_third = rates[:len(rates)//3]
                last_third = rates[-len(rates)//3:]
                
                avg_first = sum(first_third) / len(first_third)
                avg_last = sum(last_third) / len(last_third)
                
                if avg_last > avg_first * 1.1:
                    trends['hallucination_trend'] = 'increasing'
                elif avg_last < avg_first * 0.9:
                    trends['hallucination_trend'] = 'decreasing'
            
            # Calculate volume trend
            volumes = [daily['total_responses'] for daily in daily_data]
            if len(volumes) >= 3:
                first_third = volumes[:len(volumes)//3]
                last_third = volumes[-len(volumes)//3:]
                
                avg_first_vol = sum(first_third) / len(first_third)
                avg_last_vol = sum(last_third) / len(last_third)
                
                if avg_last_vol > avg_first_vol * 1.2:
                    trends['volume_trend'] = 'increasing'
                elif avg_last_vol < avg_first_vol * 0.8:
                    trends['volume_trend'] = 'decreasing'
            
        except Exception as e:
            logger.error(f"Error calculating trends: {e}")
        
        return trends

    def prepare_daily_breakdown(self, daily_data: List[Dict]) -> List[Dict]:
        """Prepare daily breakdown for frontend display"""
        breakdown = []
        
        for daily in daily_data:
            day_stats = {
                'date': daily['date'],
                'total_responses': daily['total_responses'],
                'hallucination_count': daily['hallucination_count'],
                'hallucination_rate': 0.0,
                'avg_confidence': 0.0
            }
            
            if daily['total_responses'] > 0:
                day_stats['hallucination_rate'] = daily['hallucination_count'] / daily['total_responses']
            
            if daily.get('confidence_scores'):
                day_stats['avg_confidence'] = sum(daily['confidence_scores']) / len(daily['confidence_scores'])
            
            breakdown.append(day_stats)
        
        # Sort by date (newest first)
        breakdown.sort(key=lambda x: x['date'], reverse=True)
        
        return breakdown

    async def get_recent_validations(self, limit: int = 50) -> List[Dict]:
        """Get recent validation results for detailed analysis"""
        try:
            # Get validation logs from the past 7 days
            validations = []
            end_date = datetime.utcnow()
            
            for i in range(7):
                date = end_date - timedelta(days=i)
                date_str = date.strftime('%Y-%m-%d')
                
                # Get all validation logs for this date
                pattern = f"validation_log:{date_str}:*"
                keys = await self.redis_service.redis.keys(pattern)
                
                for key in keys[:limit]:  # Limit per day
                    try:
                        log_data = await self.redis_service.redis.get(key)
                        if log_data:
                            validation = json.loads(log_data)
                            validations.append(validation)
                    except Exception as e:
                        logger.error(f"Error processing validation log {key}: {e}")
                
                if len(validations) >= limit:
                    break
            
            # Sort by timestamp (newest first)
            validations.sort(key=lambda x: x['timestamp'], reverse=True)
            
            return validations[:limit]
            
        except Exception as e:
            logger.error(f"Error getting recent validations: {e}")
            return []

    async def export_analytics_data(self, start_date: str, end_date: str) -> Dict:
        """Export analytics data for a date range"""
        try:
            # Parse dates
            start = datetime.strptime(start_date, '%Y-%m-%d')
            end = datetime.strptime(end_date, '%Y-%m-%d')
            
            export_data = {
                'period': f"{start_date} to {end_date}",
                'generated_at': datetime.utcnow().isoformat(),
                'daily_summaries': [],
                'detailed_logs': []
            }
            
            # Get daily summaries
            current_date = start
            while current_date <= end:
                date_str = current_date.strftime('%Y-%m-%d')
                summary_key = f"daily_summary:{date_str}"
                
                summary_data = await self.redis_service.redis.get(summary_key)
                if summary_data:
                    export_data['daily_summaries'].append(json.loads(summary_data))
                
                current_date += timedelta(days=1)
            
            return export_data
            
        except Exception as e:
            logger.error(f"Error exporting analytics data: {e}")
            return {'error': str(e)} 