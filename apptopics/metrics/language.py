"""Is a search in another language than English? (Step 2 review, 2026-11-06)

Spanish, Portuguese, French, German, Italian and Turkish searches ('foto', 'traductor', 'juegos para niños') are real
US-store searches, and Step 2 grades them against the market of the apps Apple shows (mostly English apps). Building
for them means a translated app, so they are tagged and hidden by default (a soft hide: the grade is kept and shown
with "Show hidden"). Non-Latin scripts are handled by `latin_only` already.

Rule (free, no API): a search is other-language when
  - it has a letter outside plain English (ñ, á, ç, ü, ğ, ...), or
  - one of its words is a common non-English word (everyday app words and unambiguous function words, list below;
    short ambiguous words like 'y', 'la', 'un' only count next to another foreign word).
A dictionary rule (every word unknown) was tried and dropped: it caught English compounds ('lockdown', 'stepup').
"""
from __future__ import annotations

import re

WEAK = {"y", "la", "il", "di", "et", "mi", "tu", "su", "al", "un", "ver", "per", "des", "de", "en", "el", "le", "les", "du"}
_WORDS = """
de del la las los el en para con por sin y una uno un al mi tu su gratis juego juegos fotos foto descargar aplicacion
aplicación traductor traducir musica música peliculas películas ver videos? niños ninos niñas ninas bebe bebé dibujar
dibujos colorear letras calculadora prestamos préstamos despertador alarma reloj camara cámara editar editor? juegos
gratuito gratuitos español espanol ingles inglés escuela noticias tiempo clima mapa mapas tienda compras comida recetas
dinero ganar trabajo citas amigos chat? biblia oracion oración dios rezar santo santos virgen catolica catolico
jogos jogo grátis baixar aplicativo musicas filmes ver fotos traduzir tradutor desenhar criança crianças você
du le les des et pour avec sans jeu jeux gratuit gratuite télécharger musique photo? dessin enfants jour verset prière
und mit für ohne spiele spiel kostenlos kostenlose herunterladen musik bilder kinder
il di per gioco giochi gratis scaricare musica bambini
ücretsiz oyun oyunlar indir müzik çeviri resim için
"""
FOREIGN = set(_WORDS.split())
FOREIGN = {w for w in FOREIGN if not w.endswith("?")} - WEAK
NON_ENGLISH_LETTER = re.compile(r"[^\x00-\x7f]")


def other_language(keyword: str) -> bool:
    k = keyword.lower()
    if not all(ord(ch) < 0x250 for ch in k):
        return False                                         # non-Latin script: handled by latin_only
    if NON_ENGLISH_LETTER.search(k):
        return True
    words = re.findall(r"[a-z]+", k)
    hits = [w for w in words if w in FOREIGN]
    weak = [w for w in words if w in WEAK]
    return bool(hits) or len(set(weak)) >= 2
