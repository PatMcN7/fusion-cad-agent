import unittest
from types import SimpleNamespace
from fusion_cad import editing
from fusion_cad.errors import ArgumentError


class Collection:
    def __init__(self, entries=()): self.entries=list(entries)
    @property
    def count(self): return len(self.entries)
    def item(self,i): return self.entries[i]


class NativeEditing(unittest.TestCase):
    def setUp(self):
        self.timeline=Collection()
        self.timeline.markerPosition=0
        self.design=SimpleNamespace(parentDocument=object(),timeline=self.timeline,allComponents=Collection())
        self.design.findEntityByToken=lambda token:[i.entity for i in self.timeline.entries if i.entity.entityToken==token]
        editing.invalidate_undo()
        editing._refs.clear()
        self.add('plate');self.add('sketch');self.add('cut')

    def add(self,name):
        entry=SimpleNamespace(index=self.timeline.count)
        entity=SimpleNamespace(name=name,entityToken=name,isValid=True,objectType='adsk::fusion::ExtrudeFeature',parentComponent=SimpleNamespace(name='root'),timelineObject=entry)
        entry.entity=entity
        def delete():
            self.timeline.entries.remove(entry);self.timeline.markerPosition-=1;entity.isValid=False;return True
        entity.deleteMe=delete
        self.timeline.entries.append(entry);self.timeline.markerPosition+=1
        return editing.ref(self.design,entity)

    def test_construction_planes_use_component_property(self):
        plane=self.timeline.item(0).entity
        plane.component=plane.parentComponent
        del plane.parentComponent
        plane.objectType='adsk::fusion::ConstructionPlane'
        self.assertEqual(editing.row(self.design,plane)['component'],'root')
        self.assertEqual(len(editing.tree(self.design,component='root')['entries']),3)

    def test_wrong_document_ref_is_rejected(self):
        key=editing.ref(self.design,self.timeline.item(2).entity)
        self.design.parentDocument=object()
        with self.assertRaises(ArgumentError): editing.resolve(self.design,key)

    def test_deleting_earlier_sketch_does_not_cascade_into_unselected_cut(self):
        key=editing.ref(self.design,self.timeline.item(1).entity)
        with self.assertRaisesRegex(ArgumentError,'Later'): editing.delete(self.design,[key])
        self.assertEqual(self.timeline.count,3)

    def test_scoped_undo_finishes_before_result_is_returned(self):
        refs=[editing.ref(self.design,self.timeline.item(i).entity) for i in (1,2)]
        editing.remember(self.design,refs)
        result=editing.undo_last(self.design)
        self.assertEqual(result['completion'],'completed')
        self.assertEqual(self.timeline.count,1)
        self.assertEqual(result['deleted'],['cut','sketch'])

    def test_intervening_feature_prevents_undo(self):
        editing.remember(self.design,[editing.ref(self.design,self.timeline.item(2).entity)])
        self.add('manual feature')
        with self.assertRaisesRegex(ArgumentError,'Design changed'): editing.undo_last(self.design)
        self.assertEqual(self.timeline.count,4)
