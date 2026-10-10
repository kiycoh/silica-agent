from silica.kernel.recall.cooccurrence import CooccurStore, build_index
from silica.kernel.text.language import detect as detect_lang


def test_detect_empty_defaults_english():
    assert detect_lang("") == "english"
    assert detect_lang("   \n  ") == "english"


def test_build_index_auto_freezes_detected_lang(tmp_path):
    store = CooccurStore(path=tmp_path / "cooccur.json", lang="auto")
    notes = [
        ("reti/intro.md", "Intro",
         "La rete trasporta i dati tra i nodi che compongono il sistema distribuito."),
        ("reti/tcp.md", "TCP",
         "Il protocollo garantisce che i pacchetti arrivino senza perdite nella rete."),
    ]
    build_index(notes, store=store, lang="auto")

    # lingua rilevata e congelata nello store
    assert store.lang == "italian"
    # le stopword italiane sono state filtrate al build (no junk nei nodi)
    stems = store.top_stems(20)
    assert "che" not in stems
    assert "rete" in [s.lower() for s in stems]


def test_latex_math_does_not_vote_for_english():
    # A LaTeX-heavy Italian lecture read "english": inside math every symbol is
    # a single letter, and d, s, t, y are English stopwords (2026-10-05, the
    # Machine Learning course's Lezione 3, first 4,000 characters).
    text = (
        "Autovettore di una matrice:\n\n"
        "$$\nA \\in \\mathbb {R} ^ {n \\times n}, \\text { autovettore di } A: v \\in \\mathbb {R} ^ {n}\n$$\n\n"
        "$$\n\\{\\lambda_ {1}, \\lambda_ {2},.., \\lambda_ {n} \\} \\quad A = V d i a g (\\lambda) V ^ {- 1}\n$$\n\n"
        "$$\n\\boldsymbol {D} = \\boldsymbol {M} ^ {T} \\boldsymbol {A} \\boldsymbol {M} \\quad s t y d\n$$\n\n"
        "Matrice singolare: se ha almeno un autovalore nullo, e la matrice non è invertibile."
    )
    assert detect_lang(text) == "italian"


def test_html_tables_and_image_links_do_not_vote_for_english():
    # MinerU writes tables as HTML: <td> and <tr> are English stopwords in the
    # stop_words list, 60 votes in the first 4,000 characters of Lezione 11.
    rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{a and b}</td></tr>" for a in (0, 1) for b in (0, 1))
    text = (f"<table>{rows * 3}</table>\n\n"
            "![](images/498ad6766852a3b87e2770b5229d025bc76d3dc354f10bd2cc3f3671d47a46c1.jpg)\n\n"
            "Vediamo se la funzione And è implementabile tramite un separatore lineare: lo XOR non lo è.")
    assert detect_lang(text) == "italian"


def test_single_letter_variables_latex_commands_and_image_captions_do_not_vote():
    # Outside math delimiters a lecture still names x, p, f and writes \text or
    # \begin; MinerU's <details> blocks hold machine captions of figures, often
    # English. All three tipped Italian lectures to "english" (2026-10-05).
    text = ("Sia x un vettore e p la sua probabilità: f(x) = p x, s t y d z.\n"
            "\\begin{array} \\text \\right \\mu \\begin \\right \\text\n"
            "<details>\n<summary>line</summary>\n\n| Capacity | Training error |\n"
            "This chart shows the error of the model as it is given more data and the curve is seen from the top.\n"
            "</details>\n\n"
            "Il modello deve avere una capacità appropriata rispetto alla complessità del problema.")
    assert detect_lang(text) == "italian"


def test_words_the_english_list_shares_with_italian_do_not_tie_an_italian_lecture():
    # The stop_words English list holds il, la, un, con, come, non, per and se,
    # so an Italian slide with an English title ("one versus all") tied 36 to 36
    # and the tie went to English (2026-10-06, Lezione 8, first 4,000
    # characters: the window nucleation reads the language from).
    text = ("Classificazione one versus all, one versus one.\n\n"
            "Se il vettore w non è nullo, la funzione segno come classificatore per i punti con un margine "
            "ad ogni passo: il punto x a distanza d da r, la retta per un iperpiano.")
    assert detect_lang(text) == "italian"


def test_french_elision_letters_do_not_vote_for_variables():
    # The French list holds c, d, j, l, m, n, s and t, the elided forms of
    # c', d', j'...; in an exercise they are variables, and Lezione 26 read
    # French on its first 4,000 characters (2026-10-06).
    text = ("Esercizio. Siano n i campioni, m le classi, d la dimensione, t il tempo, s lo scarto, "
            "c la costante e j l indice: per ogni j in 1..n calcola s(j) e t(j); se d > m allora c = n m.")
    assert detect_lang(text) == "italian"
