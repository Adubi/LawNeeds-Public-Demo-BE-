"""
Location service for geo-matching lawyers with users
Currently uses simple distance calculation, can be enhanced with Google Maps API
"""

import math
from typing import Dict, Tuple, Optional, List
import logging

logger = logging.getLogger(__name__)

# Major UK cities with coordinates (for demo purposes)
UK_CITIES = {
    'london': {'lat': 51.5074, 'lng': -0.1278, 'display': 'London'},
    'manchester': {'lat': 53.4808, 'lng': -2.2426, 'display': 'Manchester'},
    'birmingham': {'lat': 52.4862, 'lng': -1.8904, 'display': 'Birmingham'},
    'leeds': {'lat': 53.8008, 'lng': -1.5491, 'display': 'Leeds'},
    'glasgow': {'lat': 55.8642, 'lng': -4.2518, 'display': 'Glasgow'},
    'liverpool': {'lat': 53.4084, 'lng': -2.9916, 'display': 'Liverpool'},
    'edinburgh': {'lat': 55.9533, 'lng': -3.1883, 'display': 'Edinburgh'},
    'bristol': {'lat': 51.4545, 'lng': -2.5879, 'display': 'Bristol'},
    'cardiff': {'lat': 51.4816, 'lng': -3.1791, 'display': 'Cardiff'},
    'newcastle': {'lat': 54.9783, 'lng': -1.6178, 'display': 'Newcastle'},
    'sheffield': {'lat': 53.3811, 'lng': -1.4701, 'display': 'Sheffield'},
    'nottingham': {'lat': 52.9548, 'lng': -1.1581, 'display': 'Nottingham'},
    'leicester': {'lat': 52.6369, 'lng': -1.1398, 'display': 'Leicester'},
    'oxford': {'lat': 51.7520, 'lng': -1.2577, 'display': 'Oxford'},
    'cambridge': {'lat': 52.2053, 'lng': 0.1218, 'display': 'Cambridge'}
}

class LocationService:
    """Service for handling location-based operations"""
    
    @staticmethod
    def get_city_coordinates(city_name: str) -> Optional[Tuple[float, float]]:
        """Get coordinates for a UK city"""
        city_key = city_name.lower().strip()
        if city_key in UK_CITIES:
            city_data = UK_CITIES[city_key]
            return (city_data['lat'], city_data['lng'])
        return None
    
    @staticmethod
    def calculate_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """
        Calculate distance between two points using Haversine formula
        Returns distance in kilometers
        """
        # Earth's radius in kilometers
        R = 6371
        
        # Convert to radians
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lng = math.radians(lng2 - lng1)
        
        # Haversine formula
        a = (math.sin(delta_lat / 2) ** 2 + 
             math.cos(lat1_rad) * math.cos(lat2_rad) * 
             math.sin(delta_lng / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        distance = R * c
        return distance
    
    @staticmethod
    def infer_location_from_text(text: str) -> Optional[Dict[str, any]]:
        """
        Infer location from text message
        Returns city data if found
        """
        text_lower = text.lower()
        
        for city_key, city_data in UK_CITIES.items():
            if city_key in text_lower or city_data['display'].lower() in text_lower:
                return {
                    'city': city_data['display'],
                    'lat': city_data['lat'],
                    'lng': city_data['lng']
                }
        
        return None
    
    @staticmethod
    def get_nearby_cities(lat: float, lng: float, radius_km: int = 50) -> List[Dict[str, any]]:
        """Get cities within a certain radius"""
        nearby = []
        
        for city_key, city_data in UK_CITIES.items():
            distance = LocationService.calculate_distance(
                lat, lng, city_data['lat'], city_data['lng']
            )
            
            if distance <= radius_km:
                nearby.append({
                    'city': city_data['display'],
                    'distance_km': round(distance, 1),
                    'lat': city_data['lat'],
                    'lng': city_data['lng']
                })
        
        # Sort by distance
        nearby.sort(key=lambda x: x['distance_km'])
        return nearby
    
    @staticmethod
    def get_user_location_from_ip(ip_address: str) -> Optional[Dict[str, any]]:
        """
        Get approximate location from IP address
        This is a placeholder - in production, use a geolocation API
        """
        # For now, return London as default
        return {
            'city': 'London',
            'lat': UK_CITIES['london']['lat'],
            'lng': UK_CITIES['london']['lng']
        }
    
    @staticmethod
    def format_location_for_display(location_data: Dict[str, any]) -> str:
        """Format location data for user display"""
        if 'city' in location_data:
            return location_data['city']
        elif 'lat' in location_data and 'lng' in location_data:
            return f"Location ({location_data['lat']:.4f}, {location_data['lng']:.4f})"
        return "Unknown location"

# Enhanced location inference for legal context
class LegalLocationInference:
    """Infer location from legal context in messages"""
    
    # Court locations that might indicate user location
    COURT_LOCATIONS = {
        'royal courts of justice': 'london',
        'old bailey': 'london',
        'manchester crown court': 'manchester',
        'birmingham crown court': 'birmingham',
        'leeds crown court': 'leeds',
        'cardiff crown court': 'cardiff',
        'glasgow sheriff court': 'glasgow',
        'edinburgh sheriff court': 'edinburgh'
    }
    
    # Immigration office locations
    IMMIGRATION_OFFICES = {
        'lunar house': 'london',
        'croydon': 'london',
        'liverpool ukvi': 'liverpool',
        'glasgow home office': 'glasgow',
        'belfast home office': 'belfast',
        'cardiff home office': 'cardiff'
    }
    
    @classmethod
    def infer_from_legal_context(cls, text: str) -> Optional[Dict[str, any]]:
        """Infer location from legal context mentions"""
        text_lower = text.lower()
        
        # Check court mentions
        for court, city in cls.COURT_LOCATIONS.items():
            if court in text_lower:
                return LocationService.get_city_coordinates(city)
        
        # Check immigration office mentions
        for office, city in cls.IMMIGRATION_OFFICES.items():
            if office in text_lower:
                return LocationService.get_city_coordinates(city)
        
        return None 