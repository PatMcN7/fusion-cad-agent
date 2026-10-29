import unittest
from recipes.centered_face_hole import center_and_extent

class CenteredFaceHoleTests(unittest.TestCase):
    def test_selected_positive_y_face_center_and_through_reach(self):
        center, extent = center_and_extent([0,25.4,0], [25.4,25.4,25.4], [0,0,0], [25.4,25.4,25.4])
        self.assertEqual(center, [12.7,25.4,12.7])
        self.assertGreater(extent/2, 25.4)

    def test_translated_negative_x_face(self):
        center, extent = center_and_extent([-30,10,20], [-30,18,26], [-30,10,20], [-10,18,26])
        self.assertEqual(center, [-30,14,23])
        self.assertGreater(extent/2, 20)

if __name__ == '__main__':
    unittest.main()
