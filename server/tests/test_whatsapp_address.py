"""Separação de endereços brasileiros digitados em uma mensagem."""
import unittest

from server.whatsapp_address import parse, combined


class WhatsAppAddressTests(unittest.TestCase):
    def test_common_complete_formats(self):
        examples = [
            'Rua das Flores, 123, Jardim Alvorada',
            'Rua das Flores; 123; Jardim Alvorada',
            'Rua das Flores\n123\nJardim Alvorada',
            'Rua das Flores 123 Jardim Alvorada',
            'Rua das Flores 123 - Jardim Alvorada',
            'Rua das Flores, 123 Jardim Alvorada',
            'Rua das Flores, nº 123, bairro Jardim Alvorada',
            'Rua das Flores número 123 bairro Jardim Alvorada',
            'Rua: das Flores\nNúmero: 123\nBairro: Jardim Alvorada',
            'Rua: Rua das Flores, Número:123, Bairro:Jardim Alvorada',
            'Endereço: Rua das Flores, 123, Jardim Alvorada',
            'Rua das Flores, 123, Jardim Alvorada, Maringá',
        ]
        expected = {'street': 'Rua das Flores', 'number': '123', 'neighborhood': 'Jardim Alvorada'}
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(parse(text), expected)

    def test_numbers_in_street_names_are_not_house_numbers(self):
        for text in ('Rua 7 de Setembro, 120, Centro', 'Rua 7 de Setembro 120 Centro'):
            with self.subTest(text=text):
                self.assertEqual(parse(text), {'street': 'Rua 7 de Setembro', 'number': '120', 'neighborhood': 'Centro'})
        self.assertEqual(parse('Rua Bairro Alto, 123, Centro'), {'street': 'Rua Bairro Alto', 'number': '123', 'neighborhood': 'Centro'})

    def test_explicit_partial_fields_are_preserved_without_guessing(self):
        self.assertEqual(parse('Rua das Flores, 123'), {'street': 'Rua das Flores', 'number': '123'})
        self.assertEqual(parse('Rua das Flores, Jardim Alvorada'), {'street': 'Rua das Flores', 'neighborhood': 'Jardim Alvorada'})
        self.assertEqual(parse('Rua das Flores, Bairro: Zona 7'), {'street': 'Rua das Flores', 'neighborhood': 'Zona 7'})
        self.assertEqual(parse('Rua das Flores, bairro Zona 7'), {'street': 'Rua das Flores', 'neighborhood': 'Zona 7'})
        self.assertEqual(parse('Número: 123; Bairro: Centro'), {'number': '123', 'neighborhood': 'Centro'})

    def test_no_number_letter_suffix_and_explicit_complement(self):
        for house, expected in [('S/N', 'S/N'), ('sem número', 'S/N'), ('12A', '12A'), ('12-A', '12-A')]:
            with self.subTest(house=house):
                self.assertEqual(parse(f'Rua São José, {house}, Centro'), {'street': 'Rua São José', 'number': expected, 'neighborhood': 'Centro'})
        self.assertEqual(parse('Rua: São José; Número: 12; Bairro: Centro; Complemento: Casa dos fundos'),
                         {'street': 'Rua São José', 'number': '12', 'neighborhood': 'Centro', 'complement': 'Casa dos fundos'})

    def test_unknown_ambiguous_and_invalid_inputs_are_not_guessed(self):
        for text in ('Rua teste', '5', 'Centro', 'Zona 7', 'Jardim 3', 'Rua, 5, Centro', 'Rua teste, 5, Centro, outra informação',
                     'Rua X 123 Centro 2 Residencial', 'Rua teste, 5, ' + 'A' * 101,
                     'Rua: A; Número: 123 bloco B; Bairro: Centro',
                     'Rua: A; Número: 123; Número: 456; Bairro: Centro', 'X' * 601,
                     'Rua X, 5, Centro\x00'):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))
        self.assertTrue(combined('Rua X 123 Centro 2 Residencial'))


if __name__ == '__main__':
    unittest.main()
